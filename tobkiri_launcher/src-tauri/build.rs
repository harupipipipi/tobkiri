use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::Command;

#[path = "src/artifact_integrity.rs"]
mod artifact_integrity;
#[path = "src/packaged_source.rs"]
mod packaged_source;
#[path = "src/packaging_toolchain.rs"]
mod packaging_toolchain;
#[path = "src/runtime_resource_paths.rs"]
mod runtime_resource_paths;
#[allow(dead_code)]
#[path = "src/sealed_python_protocol.rs"]
mod sealed_python_protocol;

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use ed25519_dalek::Signer;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use rand::RngCore;
use sha1::Sha1;
use sha2::{Digest, Sha256};

const APP_SOURCE_DIR: &str = "tobkiri_runtime";
const SOURCE_AUTHORITY_PATH: &str = "tobkiri_runtime/docs/PACK_ARCHITECTURE_DESIGN_INPUTS.json";
const PRESENTATION_RELEASE_ROOT_ENV: &str = "TOBKIRI_PRESENTATION_RELEASE_ROOT";
const PRESENTATION_CATALOG_FILENAME: &str = "presentation_catalog.json";
const PRESENTATION_RELEASE_FILENAME: &str = "presentation_release.v4.json";
const PRESENTATION_INDEX_FILENAME: &str = "shell_artifact_index.v4.json";
const PRESENTATION_LOCK_FILENAME: &str = "shell_profile_lock.v4.json";
const RUNTIME_RESOURCE_MANIFEST: &str = "runtime-resource-manifest.v1.json";
const RUNTIME_RESOURCE_SCHEMA: &str = "io.tobkiri.runtime-resource-manifest.v1";
const SEALED_PYTHON_ROOT: &str = "python-runtime";
const SEALED_PYTHON_MANIFEST: &str = "sealed-environment.v1.json";
const SEALED_PYTHON_DIRECTORY_MODES: &str = "sealed-directory-modes.v1.json";
const PACKAGING_PYTHON_SNAPSHOT_ENV: &str = "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT";
const PACKAGING_PYTHON_INVENTORY_SHA_ENV: &str = "TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256";
const PACKAGING_SOURCE_SNAPSHOT_ENV: &str = "TOBKIRI_PACKAGING_SOURCE_SNAPSHOT";
const MACOS_ARTIFACT_POLICY_ENV: &str = "TOBKIRI_MACOS_ARTIFACT_POLICY";
const MACOS_CI_CERT_SHA256_ENV: &str = "TOBKIRI_MACOS_CI_CERT_SHA256";
const MACOS_CI_PUBLIC_KEY_ENV: &str = "TOBKIRI_MACOS_CI_PUBLIC_KEY";
const APPLE_TEAM_ID_ENV: &str = "APPLE_TEAM_ID";
const LOCAL_DEVELOPMENT_LAUNCHER_IDENTIFIER: &str = "dev.tobkiri.local-launcher";
#[cfg(target_os = "macos")]
const MACOS_XATTR_PATH: &str = "/usr/bin/xattr";
const SEALED_PYTHON_SCHEMA: &str = "io.tobkiri.sealed-python-environment.v1";
const SEALED_PYTHON_DIRECTORY_MODES_SCHEMA: &str = "io.tobkiri.sealed-python-directory-modes.v1";
const CARGO_TARGET_DIR_ENV: &str = "CARGO_TARGET_DIR";
const PANEL_BUILD_DIR_ENV: &str = "TOBKIRI_PANEL_BUILD_DIR";
const PANEL_RESOURCE_DIR: &str = "core_runtime/core_pack/core_control_panel/web";
const GENERATED_RESOURCE_DIRS: &[&str] = &[
    PANEL_RESOURCE_DIR,
    "ecosystem/defaultspack/ui",
    "bundled",
    "python-runtime",
];
const SOURCE_ONLY_PROFILE_ARTIFACTS: &[&str] = &[
    "defaults.profile.intent.v1.json",
    "defaults.profile.lock.v5.json",
    "defaults.release.provenance.json",
];
const CANONICAL_HOST_INVENTORY: &str = "canonical-files.v1.json";
const CANONICAL_HOST_INVENTORY_SCHEMA: &str = "io.tobkiri.host-file-inventory.v1";
const PRESENTATION_CATALOG_SCHEMA: &str = "io.tobkiri.launcher.presentation-catalog.v1";
const PRESENTATION_RELEASE_SCHEMA: &str = "io.tobkiri.shell.release.v4";
const PRESENTATION_INDEX_SCHEMA: &str = "io.tobkiri.shell.artifact-index.v4";
const PRESENTATION_LOCK_SCHEMA: &str = "io.tobkiri.shell.profile-lock.v4";
const ISOLATED_MODULE_CODE: &str = "import runpy,sys;source_root=sys.argv[1];module_name=sys.argv[2];sys.path.insert(0,source_root);sys.argv=[module_name,*sys.argv[3:]];runpy.run_module(module_name,run_name='__main__',alter_sys=True)";
const ISOLATED_ENVIRONMENT_KEYS: &[&str] = &[
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SystemRoot",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
];

/// Formal Rust-owned packaging boundary used by release staging.
///
/// Contract name: `tobkiri-core-package-defaults-v1`.
/// Inputs are typed below; no shell or caller-provided environment is used.
/// The source snapshot and provenance path are private implementation details:
/// the lease remains owned by this call through child exit, output validation,
/// and identity-safe cleanup. Successful output is the verified staged catalog.
const FORMAL_DEFAULTS_PACKAGING_COMMAND: &str = "tobkiri-core-package-defaults-v1";
const FORMAL_DEFAULTS_PACKAGING_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);

struct DefaultsPackagingProjection<'a> {
    source_artifact: &'a Path,
    bundle_root: &'a Path,
    artifact_root: &'a Path,
    relative_path: &'a str,
    entrypoint: &'a str,
    platform: &'a str,
    architecture: &'a str,
    bundle_identity: &'a str,
}

impl DefaultsPackagingProjection<'_> {
    fn validate(&self) -> io::Result<()> {
        for (label, path) in [
            ("source artifact", self.source_artifact),
            ("bundle root", self.bundle_root),
            ("artifact root", self.artifact_root),
        ] {
            if !path.is_absolute() {
                return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: {label} must be absolute"
                )));
            }
        }
        if safe_release_relative_path(self.relative_path, "projection relative path").is_err()
            || [
                self.entrypoint,
                self.platform,
                self.architecture,
                self.bundle_identity,
            ]
            .iter()
            .any(|value| value.is_empty() || value.contains('\0'))
        {
            return Err(invalid_release(format!(
                "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: projection text is invalid"
            )));
        }
        Ok(())
    }

    fn argv(&self) -> Vec<std::ffi::OsString> {
        [
            std::ffi::OsString::from("--source-artifact"),
            self.source_artifact.as_os_str().to_owned(),
            std::ffi::OsString::from("--bundle-root"),
            self.bundle_root.as_os_str().to_owned(),
            std::ffi::OsString::from("--artifact-root"),
            self.artifact_root.as_os_str().to_owned(),
            std::ffi::OsString::from("--relative-path"),
            std::ffi::OsString::from(self.relative_path),
            std::ffi::OsString::from("--entrypoint"),
            std::ffi::OsString::from(self.entrypoint),
            std::ffi::OsString::from("--platform"),
            std::ffi::OsString::from(self.platform),
            std::ffi::OsString::from("--architecture"),
            std::ffi::OsString::from(self.architecture),
            std::ffi::OsString::from("--bundle-identity"),
            std::ffi::OsString::from(self.bundle_identity),
            std::ffi::OsString::from("--source-provenance-file"),
            std::ffi::OsString::from("packaging-source-provenance.v1.json"),
        ]
        .into_iter()
        .collect()
    }

    fn append_argv(&self, command: &mut packaging_toolchain::VerifiedCommand<'_>) {
        command.args(self.argv());
    }
}

struct DefaultsPackagingRequest<'a> {
    repository_root: &'a Path,
    snapshot_parent: &'a Path,
    trusted_source_manifest: &'a [u8],
    source_revision: &'a str,
    source_tree: &'a str,
    projection: DefaultsPackagingProjection<'a>,
}

struct DefaultsPackagingOutput {
    default_profile_sha256: String,
    defaultspack_lock_sha256: String,
}

#[derive(serde::Serialize)]
struct SourceProvenance<'a> {
    schema: &'static str,
    source_commit: &'a str,
    source_tree: &'a str,
    source_clean: bool,
    source_manifest_sha256: String,
}

#[cfg(not(test))]
fn main() {
    println!("cargo:rerun-if-changed=splash/index.html");
    println!("cargo:rerun-if-changed=splash/tobkiri_launcher_startup_blade_cut.svg");
    println!("cargo:rerun-if-changed=src/lib.rs");
    println!("cargo:rerun-if-changed=../../pack-shell/Cargo.toml");
    println!("cargo:rerun-if-changed=../../pack-shell/src");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/app.py");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/core_runtime");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/tobkiri_host");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/ecosystem");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/flows");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/lang");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/requirements.txt");
    println!("cargo:rerun-if-changed=bundled");
    println!("cargo:rerun-if-changed=bundled/presentation_catalog.json");
    println!("cargo:rerun-if-env-changed={PRESENTATION_RELEASE_ROOT_ENV}");
    println!("cargo:rerun-if-env-changed={PANEL_BUILD_DIR_ENV}");
    println!("cargo:rerun-if-env-changed={PACKAGING_PYTHON_SNAPSHOT_ENV}");
    println!("cargo:rerun-if-env-changed={PACKAGING_PYTHON_INVENTORY_SHA_ENV}");
    println!("cargo:rerun-if-env-changed={MACOS_ARTIFACT_POLICY_ENV}");
    println!("cargo:rerun-if-env-changed={MACOS_CI_CERT_SHA256_ENV}");
    println!("cargo:rerun-if-env-changed={MACOS_CI_PUBLIC_KEY_ENV}");
    println!("cargo:rerun-if-env-changed={APPLE_TEAM_ID_ENV}");
    println!("cargo:rerun-if-changed=capabilities");

    if let Some(panel_dir) = configured_panel_build_dir(&PathBuf::from(env!("CARGO_MANIFEST_DIR")))
    {
        println!("cargo:rerun-if-changed={}", panel_dir.display());
    }

    bind_macos_artifact_policy().expect("failed to bind macOS artifact policy");
    warn_legacy_defaultspack_app_bundle();
    let unbundled_local_development = is_unbundled_local_development_build();
    if unbundled_local_development {
        println!("cargo:rustc-env=TOBKIRI_LOCAL_DEV_WORKSPACE=1");
        println!(
            "cargo:warning=using the local development workspace runtime; sealed runtime staging is release-only"
        );
    } else {
        println!("cargo:rustc-env=TOBKIRI_LOCAL_DEV_WORKSPACE=0");
        stage_runtime_bundle().expect("failed to stage runtime bundle");
        reset_tauri_macos_resource_copy()
            .expect("failed to reset the manifest-bound Tauri resource copy");
        prepare_debug_tauri_resource_destination()
            .expect("failed to prepare debug Tauri resource destination");
    }
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_setup_progress",
            "debug_approval_status",
            "arm_debug_approval",
            "revoke_debug_approval",
            "restart_kernel",
            "reauthorize_panel_session",
            "open_external_url",
            "close_current_window",
            "open_authority_approval_window",
            "open_ambient_trigger_window",
            "open_finger_recording_window",
            "open_defaultspack_main_window",
            "open_defaults_console_window",
            "open_host_permissions_window",
            "authority_approval_context",
            "coding_approval_operator",
            "send_to_background",
            "show_app_window",
            "get_background_control_status",
            "get_desktop_system_info",
            "get_host_permission_status",
            "open_host_permission_settings",
            "register_defaultspack_dock",
            "launch_defaultspack_desktop",
            "get_presentation_catalog",
            "select_presentation",
            "launch_selected_presentation",
        ]),
    ))
    .expect("failed to build Tauri application manifest")
}

/// Tauri preserves the sealed source modes while copying the packaged Python
/// tree into `target/debug/app`. Restore owner write access on that generated
/// destination before the next debug copy so iterative builds can overwrite
/// it. The sealed source tree and every non-debug build remain untouched.
fn prepare_debug_tauri_resource_destination() -> io::Result<()> {
    if std::env::var("PROFILE").as_deref() != Ok("debug") {
        return Ok(());
    }
    let out_dir = PathBuf::from(
        std::env::var_os("OUT_DIR").ok_or_else(|| invalid_release("Cargo OUT_DIR is missing"))?,
    );
    let profile_dir = out_dir
        .ancestors()
        .nth(3)
        .ok_or_else(|| invalid_release("Cargo OUT_DIR has no profile directory"))?;
    let resource_root = profile_dir.join("app");
    if resource_root.exists() {
        make_generated_tree_owner_writable(&resource_root)?;
    }
    Ok(())
}

fn make_generated_tree_owner_writable(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Err(invalid_release(format!(
            "debug Tauri resource destination contains a symlink: {}",
            path.display()
        )));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(
            path,
            fs::Permissions::from_mode(metadata.permissions().mode() | 0o200),
        )?;
    }
    #[cfg(not(unix))]
    {
        let mut permissions = metadata.permissions();
        permissions.set_readonly(false);
        fs::set_permissions(path, permissions)?;
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            make_generated_tree_owner_writable(&entry?.path())?;
        }
    }
    Ok(())
}

fn bind_macos_artifact_policy() -> io::Result<()> {
    let target = required_cargo_target()?;
    let profile = required_cargo_profile()?;
    let policy =
        std::env::var(MACOS_ARTIFACT_POLICY_ENV).unwrap_or_else(|_| "production-v1".to_owned());
    let is_macos = target.ends_with("-apple-darwin");

    if !is_macos {
        if policy != "production-v1"
            || std::env::var_os(MACOS_CI_CERT_SHA256_ENV).is_some()
            || std::env::var_os(MACOS_CI_PUBLIC_KEY_ENV).is_some()
        {
            return Err(invalid_release(
                "macOS artifact policy may only be selected for an Apple Darwin target",
            ));
        }
        println!("cargo:rustc-env=TOBKIRI_MACOS_ARTIFACT_POLICY=production-v1");
        println!("cargo:rustc-env=TOBKIRI_MACOS_ARTIFACT_IDENTITY=");
        println!("cargo:rustc-env=TOBKIRI_MACOS_CI_PUBLIC_KEY=");
        return Ok(());
    }

    let identity = match policy.as_str() {
        "production-v1" => {
            if std::env::var_os(MACOS_CI_CERT_SHA256_ENV).is_some() {
                return Err(invalid_release(
                    "production macOS builds may not carry a CI signing certificate",
                ));
            }
            if std::env::var_os(MACOS_CI_PUBLIC_KEY_ENV).is_some() {
                return Err(invalid_release(
                    "production macOS builds may not carry a CI verification key",
                ));
            }
            if std::env::var_os(APPLE_TEAM_ID_ENV).is_some() {
                return Err(invalid_release(
                    "OSS macOS production builds may not claim an Apple Team ID",
                ));
            }
            String::new()
        }
        "ci-e2e-v1" => {
            if profile != "release" {
                return Err(invalid_release(
                    "the non-publishable CI/E2E policy is restricted to release-profile artifacts",
                ));
            }
            let digest = std::env::var(MACOS_CI_CERT_SHA256_ENV).map_err(|_| {
                invalid_release("CI/E2E macOS builds require TOBKIRI_MACOS_CI_CERT_SHA256")
            })?;
            if !valid_raw_sha256(&digest) {
                return Err(invalid_release(
                    "CI/E2E macOS signing certificate identity must be a lowercase SHA-256",
                ));
            }
            if std::env::var_os(APPLE_TEAM_ID_ENV).is_some() {
                return Err(invalid_release(
                    "CI/E2E macOS builds may not claim a production Apple Team ID",
                ));
            }
            let public_key = std::env::var(MACOS_CI_PUBLIC_KEY_ENV).map_err(|_| {
                invalid_release("CI/E2E macOS builds require TOBKIRI_MACOS_CI_PUBLIC_KEY")
            })?;
            let decoded = BASE64
                .decode(&public_key)
                .map_err(|_| invalid_release("CI/E2E macOS public key must be canonical base64"))?;
            if decoded.len() != 32 || BASE64.encode(decoded) != public_key {
                return Err(invalid_release(
                    "CI/E2E macOS public key must encode exactly 32 bytes",
                ));
            }
            digest
        }
        _ => return Err(invalid_release("unknown macOS artifact policy")),
    };
    println!("cargo:rustc-env=TOBKIRI_MACOS_ARTIFACT_POLICY={policy}");
    println!("cargo:rustc-env=TOBKIRI_MACOS_ARTIFACT_IDENTITY={identity}");
    println!(
        "cargo:rustc-env=TOBKIRI_MACOS_CI_PUBLIC_KEY={}",
        std::env::var(MACOS_CI_PUBLIC_KEY_ENV).unwrap_or_default()
    );
    Ok(())
}

#[cfg(test)]
fn main() {}

fn isolated_python_module_command<'a>(
    python: &'a packaging_toolchain::VerifiedTool,
    source: &packaged_source::VerifiedSourceSnapshot,
    module: &str,
) -> io::Result<packaging_toolchain::VerifiedCommand<'a>> {
    source.verify_unchanged()?;
    let mut command = python.command()?;
    command
        .env_clear()
        .args(["-I", "-B", "-c", ISOLATED_MODULE_CODE])
        .arg(source.root())
        .arg(module)
        .env(
            "GIT_CONFIG_GLOBAL",
            if cfg!(windows) { "NUL" } else { "/dev/null" },
        )
        .env("GIT_CONFIG_NOSYSTEM", "1");
    for key in ISOLATED_ENVIRONMENT_KEYS {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    source.bind_command_cwd(&mut command)?;
    #[cfg(target_os = "macos")]
    // The sealed venv's relative `home` is resolved from its verified root;
    // the source snapshot is passed as an absolute import root above.
    command.bind_python_runtime_cwd()?;
    Ok(command)
}

fn run_formal_defaults_packaging<T, F>(
    request: DefaultsPackagingRequest<'_>,
    finalize: F,
) -> io::Result<T>
where
    F: FnOnce(DefaultsPackagingOutput) -> io::Result<T>,
{
    request.projection.validate()?;
    for (label, path) in [
        ("repository root", request.repository_root),
        ("snapshot parent", request.snapshot_parent),
    ] {
        if !path.is_absolute() {
            return Err(invalid_release(format!(
                "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: {label} must be absolute"
            )));
        }
    }
    let mut source = packaged_source::verify_and_snapshot_against_manifest(
        &request.repository_root.join("tobkiri_runtime"),
        request.snapshot_parent,
        request.trusted_source_manifest,
    )
    .map_err(|error| {
        invalid_release(format!(
            "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: trusted Rust source verification failed: {error}"
        ))
    })?;
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum LeaseState {
        NoChild,
        RunningUncontained,
        Reaped,
    }
    let mut lease_state = LeaseState::NoChild;
    let execution = (|| -> io::Result<T> {
        let provenance = serde_json::to_vec(&SourceProvenance {
            schema: "io.tobkiri.packaging-source-provenance.v1",
            source_commit: request.source_revision,
            source_tree: request.source_tree,
            source_clean: true,
            source_manifest_sha256: raw_byte_digest(request.trusted_source_manifest),
        })
        .map_err(io::Error::other)?;
        source.bind_provenance(&provenance)?;
        let python = packaging_toolchain::verified_tool("python")?;
        source.verify_unchanged()?;
        let mut command = isolated_python_module_command(
            &python,
            &source,
            "scripts.generate_packaged_defaultspack_v4_bundle",
        )?;
        request.projection.append_argv(&mut command);
        lease_state = LeaseState::RunningUncontained;
        let mut child = match command.spawn_outcome() {
            packaging_toolchain::VerifiedSpawnOutcome::NoChild(error) => {
                lease_state = LeaseState::NoChild;
                return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: generator spawn failed before child creation: {error}"
                )));
            }
            packaging_toolchain::VerifiedSpawnOutcome::ReapedFailure(error) => {
                lease_state = LeaseState::Reaped;
                return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: generator was rejected and reaped: {error}"
                )));
            }
            packaging_toolchain::VerifiedSpawnOutcome::Running(child) => child,
            packaging_toolchain::VerifiedSpawnOutcome::Uncontained(error) => {
                return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: generator spawn left an uncontained child: {error}"
                )));
            }
        };
        let deadline = std::time::Instant::now() + FORMAL_DEFAULTS_PACKAGING_TIMEOUT;
        let status = match child.wait_until(deadline) {
            Ok(Some(status)) => {
                lease_state = LeaseState::Reaped;
                status
            }
            Ok(None) => {
                let kill = child.kill();
                let reap =
                    child.wait_until(std::time::Instant::now() + std::time::Duration::from_secs(2));
                if matches!(reap, Ok(Some(_))) {
                    lease_state = LeaseState::Reaped;
                }
                return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: generator timed out; kill={kill:?}; reap={reap:?}"
                )));
            }
            Err(error) => {
                return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: generator identity was lost while waiting: {error}; signaling was stopped to avoid PID reuse"
                )));
            }
        };
        if !status.success() {
            return Err(invalid_release(format!(
                "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: generator exited with {status}"
            )));
        }
        source.verify_unchanged()?;
        let profile = request
            .projection
            .bundle_root
            .join("defaults.profile.v4.json");
        let lock = request.projection.bundle_root.join("bundle.lock.json");
        let profile_digest = byte_digest(&fs::read(&profile)?);
        let lock_digest = byte_digest(&fs::read(&lock)?);
        finalize(DefaultsPackagingOutput {
            default_profile_sha256: profile_digest,
            defaultspack_lock_sha256: lock_digest,
        })
    })();
    if lease_state == LeaseState::RunningUncontained {
        std::mem::forget(source);
        return Err(match execution {
            Ok(_) => invalid_release(format!(
                "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: child containment was not proven; private snapshot retained fail-closed"
            )),
            Err(error) => invalid_release(format!(
                "{error}; {FORMAL_DEFAULTS_PACKAGING_COMMAND}: child containment was not proven, so the private snapshot was retained fail-closed"
            )),
        });
    }
    let cleanup = source.cleanup();
    match (execution, cleanup) {
        (Ok(output), Ok(())) => Ok(output),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(cleanup)) => Err(invalid_release(format!(
            "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: snapshot cleanup failed: {cleanup}"
        ))),
        (Err(error), Err(cleanup)) => Err(invalid_release(format!(
            "{error}; {FORMAL_DEFAULTS_PACKAGING_COMMAND}: snapshot cleanup also failed: {cleanup}"
        ))),
    }
}

fn warn_legacy_defaultspack_app_bundle() {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return;
    };
    let legacy_app = home.join("Applications").join("Rumi_Defaultspack.app");
    if !legacy_app.exists() {
        return;
    }

    let launch = fs::read_to_string(legacy_app.join("Contents").join("MacOS").join("launch"))
        .unwrap_or_default();
    let missing_markers = [
        "--api-token",
        "--port",
        "RUMI_LOG_DIR",
        "RUMI_DEFAULTSPACK_OPEN_BROWSER",
    ]
    .into_iter()
    .filter(|marker| !launch.contains(marker))
    .collect::<Vec<_>>();
    if missing_markers.is_empty() {
        println!(
            "cargo:warning=legacy underscore-named Defaultspack app bundle detected at {}; re-register Defaultspack from Rumi Viewer to clean it up",
            legacy_app.display()
        );
    } else {
        println!(
            "cargo:warning=legacy Defaultspack app bundle detected at {}; missing launch markers: {}; re-register Defaultspack from Rumi Viewer or remove the legacy bundle",
            legacy_app.display(),
            missing_markers.join(", ")
        );
    }
}

fn stage_runtime_bundle() -> io::Result<()> {
    let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = project_dir
        .parent()
        .and_then(Path::parent)
        .expect("src-tauri should live under tobkiri_launcher/");
    let runtime_root = repo_root.join(APP_SOURCE_DIR);
    let staged_root = project_dir.join("gen").join("app");

    reset_staged_runtime(&staged_root)
        .map_err(|error| stage_error("reset staged runtime", error))?;
    if core_build_stage() == CoreBuildStage::IntermediateShell {
        // The Shell is only the presentation artifact consumed by the outer
        // Launcher. It has no embedded Kernel/Python resources of its own, so
        // requiring the production packaging toolchain here makes an ordinary
        // unsigned developer build impossible for no security benefit.
        println!("cargo:rustc-env=TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256=");
        println!("cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_B64=");
        println!("cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_ID=");
        fs::write(
            staged_root.join("intermediate-shell-stage.v1"),
            b"io.tobkiri.intermediate-shell-stage.v1\n",
        )?;
        return write_runtime_resource_manifest(&staged_root);
    }
    if required_cargo_profile()? != "release" {
        return stage_development_runtime_bundle(
            &project_dir,
            repo_root,
            &runtime_root,
            &staged_root,
        );
    }
    if !copy_tracked_runtime_tree(repo_root, &staged_root)
        .map_err(|error| stage_error("copy tracked runtime", error))?
    {
        return Err(stage_error(
            "copy tracked runtime",
            io::Error::new(
                io::ErrorKind::NotFound,
                "git tracked runtime inventory is unavailable",
            ),
        ));
    }
    verify_canonical_host_package(&staged_root, &runtime_root)
        .map_err(|error| stage_error("verify canonical Host package", error))?;
    let sealed_python_source = configured_sealed_python_snapshot()?;
    bind_sealed_python_root(&sealed_python_source, true)
        .map_err(|error| stage_error("verify source sealed Python", error))?;
    copy_generated_resource_dirs(
        &project_dir,
        &runtime_root,
        &staged_root,
        Some(&sealed_python_source),
    )
    .map_err(|error| stage_error("copy generated resources", error))?;
    stage_setup_brand_icon(repo_root, &staged_root)
        .map_err(|error| stage_error("stage setup brand icon", error))?;

    let bundled_src = project_dir.join("bundled");
    if !bundled_src.is_dir() {
        return Err(stage_error(
            "locate Launcher bundled resources",
            io::Error::new(
                io::ErrorKind::NotFound,
                format!(
                    "bundled resource directory is missing at {}",
                    bundled_src.display()
                ),
            ),
        ));
    }
    copy_dir_recursive(&bundled_src, &staged_root.join("bundled"))
        .map_err(|error| stage_error("copy Launcher bundled resources", error))?;
    let bundled_catalog = bundled_src.join(PRESENTATION_CATALOG_FILENAME);
    let staged_catalog = staged_root
        .join("bundled")
        .join(PRESENTATION_CATALOG_FILENAME);
    let catalog_source = stage_presentation_release(&staged_root)
        .map_err(|error| stage_error("stage verified presentation artifact", error))?
        .unwrap_or(bundled_catalog);
    verify_staged_catalog(&catalog_source, &staged_catalog)
        .map_err(|error| stage_error("verify staged presentation catalog", error))?;

    rebase_staged_sealed_python(&staged_root)
        .map_err(|error| stage_error("re-seal generated Python application closure", error))?;

    stage_pack_shell(repo_root, &staged_root)
        .map_err(|error| stage_error("stage pack-shell", error))?;
    bind_sealed_python_root(&staged_root.join(SEALED_PYTHON_ROOT), false)
        .map_err(|error| stage_error("bind sealed Python environment", error))?;
    write_runtime_resource_manifest(&staged_root)
        .map_err(|error| stage_error("seal staged runtime", error))?;
    prepare_staged_macos_xattr_transport(&staged_root)
        .map_err(|error| stage_error("prepare staged macOS xattr transport", error))?;

    Ok(())
}

fn stage_development_runtime_bundle(
    project_dir: &Path,
    repo_root: &Path,
    runtime_root: &Path,
    staged_root: &Path,
) -> io::Result<()> {
    copy_dir_recursive_filtered(runtime_root, staged_root, runtime_root)
        .map_err(|error| stage_error("copy development runtime", error))?;
    verify_canonical_host_package(staged_root, runtime_root)
        .map_err(|error| stage_error("verify canonical Host package", error))?;
    copy_generated_resource_dirs(project_dir, runtime_root, staged_root, None)
        .map_err(|error| stage_error("copy development resources", error))?;
    stage_setup_brand_icon(repo_root, staged_root)
        .map_err(|error| stage_error("stage setup brand icon", error))?;

    let bundled_src = project_dir.join("bundled");
    copy_dir_recursive(&bundled_src, &staged_root.join("bundled"))
        .map_err(|error| stage_error("copy Launcher bundled resources", error))?;
    let staged_catalog = staged_root
        .join("bundled")
        .join(PRESENTATION_CATALOG_FILENAME);
    verify_staged_catalog(
        &bundled_src.join(PRESENTATION_CATALOG_FILENAME),
        &staged_catalog,
    )
    .map_err(|error| stage_error("verify development presentation catalog", error))?;

    let development_defaults = project_dir.join("target/dev-defaults");
    if development_defaults.join("v4/bundle.lock.json").is_file()
        && development_defaults.join("platform-artifacts").is_dir()
    {
        copy_dir_recursive(
            &development_defaults,
            &staged_root.join("bundled/dev-defaults"),
        )
        .map_err(|error| stage_error("stage development Defaults bundle", error))?;
    }

    stage_pack_shell(repo_root, staged_root)
        .map_err(|error| stage_error("stage development pack-shell", error))?;
    let development_venv = repo_root.join(".venv");
    if development_venv.join("bin/python3").is_file() {
        copy_development_venv_tree(&development_venv, &staged_root.join("dev-venv"))
            .map_err(|error| stage_error("stage development Python environment", error))?;
        write_development_runtime_path(&staged_root.join("dev-venv"))
            .map_err(|error| stage_error("bind development Python imports", error))?;
    }
    bind_sealed_python_root(&staged_root.join(SEALED_PYTHON_ROOT), false)
        .map_err(|error| stage_error("bind development Python environment", error))?;
    write_runtime_resource_manifest(staged_root)
        .map_err(|error| stage_error("seal staged development runtime", error))
}

#[cfg(not(target_os = "macos"))]
fn reset_tauri_macos_resource_copy() -> io::Result<()> {
    Ok(())
}

#[cfg(target_os = "macos")]
fn reset_tauri_resource_copy_at(out_dir: &Path, profile_root: &Path) -> io::Result<PathBuf> {
    reject_staged_path_components(out_dir)?;
    reject_staged_path_components(profile_root)?;
    let build_root = profile_root.join("build");
    let relative = out_dir.strip_prefix(&build_root).map_err(|_| {
        invalid_release("Cargo OUT_DIR escaped the expected target profile build root")
    })?;
    let components = relative.components().collect::<Vec<_>>();
    if components.len() != 2
        || !matches!(components[0], Component::Normal(_))
        || components[1].as_os_str() != "out"
    {
        return Err(invalid_release(
            "Cargo OUT_DIR has an unexpected target profile shape",
        ));
    }
    let resource_root = profile_root.join("app");
    reset_staged_runtime(&resource_root)?;
    Ok(resource_root)
}

#[cfg(target_os = "macos")]
fn tauri_resource_profile_root(
    out_dir: &Path,
    target_root: &Path,
    target: &str,
    profile: &str,
) -> io::Result<PathBuf> {
    validate_path_component(target, "Rust target")?;
    validate_path_component(profile, "Cargo profile")?;
    reject_staged_path_components(out_dir)?;
    reject_staged_path_components(target_root)?;

    let profile_roots = [
        target_root.join(profile),
        target_root.join(target).join(profile),
    ];
    for profile_root in profile_roots {
        let build_root = profile_root.join("build");
        let Ok(relative) = out_dir.strip_prefix(&build_root) else {
            continue;
        };
        let components = relative.components().collect::<Vec<_>>();
        if components.len() != 2
            || !matches!(components[0], Component::Normal(_))
            || components[1].as_os_str() != "out"
        {
            return Err(invalid_release(
                "Cargo OUT_DIR has an unexpected target profile shape",
            ));
        }
        return Ok(profile_root);
    }

    Err(invalid_release(
        "Cargo OUT_DIR escaped the expected target profile build roots",
    ))
}

#[cfg(target_os = "macos")]
fn reset_tauri_resource_copy_for_cargo_at(
    out_dir: &Path,
    target_root: &Path,
    target: &str,
    profile: &str,
) -> io::Result<PathBuf> {
    let profile_root = tauri_resource_profile_root(out_dir, target_root, target, profile)?;
    reset_tauri_resource_copy_at(out_dir, &profile_root)
}

#[cfg(target_os = "macos")]
fn reset_tauri_macos_resource_copy() -> io::Result<()> {
    let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let target_root = resolve_tauri_shell_target_dir(&project_dir)?;
    let target = required_cargo_target()?;
    let profile = std::env::var("PROFILE")
        .map_err(|_| invalid_release("Cargo PROFILE is missing for resource reset"))?;
    let out_dir = std::env::var_os("OUT_DIR")
        .map(PathBuf::from)
        .ok_or_else(|| invalid_release("Cargo OUT_DIR is missing for resource reset"))?;
    reset_tauri_resource_copy_for_cargo_at(&out_dir, &target_root, &target, &profile)?;
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn prepare_staged_macos_xattr_transport(_root: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(target_os = "macos")]
#[derive(Clone)]
struct MacosStagedEntryIdentity {
    path: PathBuf,
    device: u64,
    inode: u64,
    mode: u32,
    size: u64,
    directory: bool,
}

#[cfg(target_os = "macos")]
fn macos_staged_entry_identities(root: &Path) -> io::Result<Vec<MacosStagedEntryIdentity>> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    fn visit(path: &Path, entries: &mut Vec<MacosStagedEntryIdentity>) -> io::Result<()> {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};

        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink()
            || (!metadata.is_dir() && !metadata.is_file())
            || (metadata.is_file() && metadata.nlink() != 1)
            || metadata.uid() != unsafe { libc::geteuid() }
        {
            return Err(invalid_release(format!(
                "staged macOS xattr entry has unsafe identity: {}",
                path.display()
            )));
        }
        entries.push(MacosStagedEntryIdentity {
            path: path.to_path_buf(),
            device: metadata.dev(),
            inode: metadata.ino(),
            mode: metadata.permissions().mode() & 0o777,
            size: metadata.len(),
            directory: metadata.is_dir(),
        });
        if metadata.is_dir() {
            let mut children = fs::read_dir(path)?.collect::<Result<Vec<_>, _>>()?;
            children.sort_by_key(fs::DirEntry::file_name);
            for child in children {
                visit(&child.path(), entries)?;
            }
        }
        Ok(())
    }

    let metadata = fs::symlink_metadata(MACOS_XATTR_PATH)?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.uid() != 0
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0
        || Path::new(MACOS_XATTR_PATH).canonicalize()? != Path::new(MACOS_XATTR_PATH)
    {
        return Err(invalid_release(
            "canonical macOS xattr tool identity is unsafe",
        ));
    }

    let mut entries = Vec::new();
    visit(root, &mut entries)?;
    Ok(entries)
}

#[cfg(target_os = "macos")]
fn verify_macos_staged_transport(entries: &[MacosStagedEntryIdentity]) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    for entry in entries {
        let metadata = fs::symlink_metadata(&entry.path)?;
        if metadata.file_type().is_symlink()
            || metadata.dev() != entry.device
            || metadata.ino() != entry.inode
            || metadata.permissions().mode() & 0o777
                != if entry.directory {
                    entry.mode | 0o700
                } else {
                    entry.mode | 0o200
                }
            || metadata.len() != entry.size
            || metadata.is_dir() != entry.directory
        {
            return Err(invalid_release(format!(
                "staged macOS entry changed during xattr transport preparation: {}",
                entry.path.display()
            )));
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn restore_macos_staged_modes(entries: &[MacosStagedEntryIdentity]) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    for entry in entries.iter().rev() {
        let metadata = fs::symlink_metadata(&entry.path)?;
        if metadata.file_type().is_symlink()
            || metadata.dev() != entry.device
            || metadata.ino() != entry.inode
            || metadata.len() != entry.size
            || metadata.is_dir() != entry.directory
        {
            return Err(invalid_release(format!(
                "staged macOS entry changed before mode rollback: {}",
                entry.path.display()
            )));
        }
        fs::set_permissions(&entry.path, fs::Permissions::from_mode(entry.mode))?;
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn prepare_staged_macos_xattr_transport(root: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let entries = macos_staged_entry_identities(root)?;
    for entry in &entries {
        let writable_mode = if entry.directory {
            entry.mode | 0o700
        } else {
            entry.mode | 0o200
        };
        if let Err(error) =
            fs::set_permissions(&entry.path, fs::Permissions::from_mode(writable_mode))
        {
            restore_macos_staged_modes(&entries)?;
            return Err(error);
        }
    }

    let output = Command::new(MACOS_XATTR_PATH)
        .env_clear()
        .args(["-c", "-r"])
        .arg(root)
        .output()?;
    verify_macos_staged_transport(&entries)?;
    if !output.status.success() {
        restore_macos_staged_modes(&entries)?;
        return Err(invalid_release(format!(
            "canonical macOS xattr transport probe failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(())
}

fn collect_runtime_resource_files(root: &Path, current: &Path) -> io::Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "staged runtime resource may not be a symlink: {}",
                    path.display()
                ),
            ));
        }
        if metadata.is_dir() {
            files.extend(collect_runtime_resource_files(root, &path)?);
        } else if metadata.is_file()
            && path.file_name().and_then(|name| name.to_str()) != Some(RUNTIME_RESOURCE_MANIFEST)
        {
            reject_release_hardlink(&metadata, &path)?;
            files.push(path.strip_prefix(root).unwrap_or(&path).to_path_buf());
        } else if !metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "staged runtime resource may not be special: {}",
                    path.display()
                ),
            ));
        }
    }
    files.sort_by_key(|path| portable_relative_path(path));
    Ok(files)
}

fn collect_runtime_resource_directories(root: &Path, current: &Path) -> io::Result<Vec<String>> {
    let mut directories = Vec::new();
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "staged runtime resource may not be a symlink: {}",
                    path.display()
                ),
            ));
        }
        if metadata.is_dir() {
            directories.push(portable_relative_path(
                path.strip_prefix(root).unwrap_or(&path),
            ));
            directories.extend(collect_runtime_resource_directories(root, &path)?);
        } else if !metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "staged runtime resource may not be special: {}",
                    path.display()
                ),
            ));
        }
    }
    directories.sort();
    Ok(directories)
}

fn rebase_staged_sealed_python(staged_root: &Path) -> io::Result<()> {
    if core_build_stage() != CoreBuildStage::FinalApplication
        || required_cargo_profile()? != "release"
        || std::env::var("DEP_TAURI_DEV").ok().as_deref() == Some("true")
    {
        return Ok(());
    }
    let source_snapshot = PathBuf::from(
        std::env::var_os(PACKAGING_SOURCE_SNAPSHOT_ENV).ok_or_else(|| {
            invalid_release(format!("{PACKAGING_SOURCE_SNAPSHOT_ENV} is required"))
        })?,
    );
    if !source_snapshot.is_absolute() {
        return Err(invalid_release(format!(
            "{PACKAGING_SOURCE_SNAPSHOT_ENV} must be absolute"
        )));
    }
    require_directory(&source_snapshot, "packaging source snapshot")?;
    let script = source_snapshot.join(".github/scripts/build_sealed_python_environment.py");
    require_regular_file(&script, "sealed Python application re-seal producer")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::symlink_metadata(&source_snapshot)?.permissions().mode() & 0o222 != 0
            || fs::symlink_metadata(&script)?.permissions().mode() & 0o222 != 0
        {
            return Err(invalid_release(
                "sealed Python application re-seal producer is writable",
            ));
        }
    }
    let target = required_cargo_target()?;
    let expected = std::env::var(PACKAGING_PYTHON_INVENTORY_SHA_ENV).map_err(|_| {
        invalid_release(format!("{PACKAGING_PYTHON_INVENTORY_SHA_ENV} is required"))
    })?;
    if !valid_raw_sha256(&expected) {
        return Err(invalid_release(
            "formal sealed Python inventory binding is invalid",
        ));
    }
    let sealed_root = staged_root.join(SEALED_PYTHON_ROOT);
    let work_budget = sealed_python_reseal_work_budget(&sealed_root, &expected)?;
    let python = packaging_toolchain::verified_tool("python")?;
    let mut command = python.command()?;
    command.args([
        std::ffi::OsString::from("-B"),
        script.as_os_str().to_owned(),
        std::ffi::OsString::from("--target"),
        std::ffi::OsString::from(target),
        std::ffi::OsString::from("--output-root"),
        sealed_root.as_os_str().to_owned(),
        std::ffi::OsString::from("--base-root"),
        sealed_root.as_os_str().to_owned(),
        std::ffi::OsString::from("--expected-base-manifest-sha256"),
        std::ffi::OsString::from(expected),
        std::ffi::OsString::from("--rebase-application-source"),
        staged_root.as_os_str().to_owned(),
    ]);
    let output = command.output_with_budget(work_budget)?;
    if !output.status.success() {
        return Err(invalid_release(format!(
            "sealed Python application re-seal failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    let stdout = String::from_utf8(output.stdout)
        .map_err(|_| invalid_release("sealed Python application re-seal output is not UTF-8"))?;
    let marker = stdout
        .lines()
        .find_map(|line| line.strip_prefix("TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256="))
        .ok_or_else(|| invalid_release("sealed Python application re-seal identity is missing"))?;
    if !valid_raw_sha256(marker)
        || raw_byte_digest(&read_regular_file(
            &sealed_root.join(SEALED_PYTHON_MANIFEST),
            "re-sealed Python manifest",
        )?) != marker
    {
        return Err(invalid_release(
            "sealed Python application re-seal identity does not match its manifest",
        ));
    }
    Ok(())
}

fn sealed_python_reseal_work_budget(
    sealed_root: &Path,
    expected_manifest_sha256: &str,
) -> io::Result<packaging_toolchain::VerifiedOutputBudget> {
    let manifest_path = sealed_root.join(SEALED_PYTHON_MANIFEST);
    require_regular_file(&manifest_path, "re-seal work inventory")?;
    let manifest = fs::read(&manifest_path)?;
    if !valid_raw_sha256(expected_manifest_sha256)
        || raw_byte_digest(&manifest) != expected_manifest_sha256
    {
        return Err(invalid_release(
            "re-seal work inventory differs from the formal binding",
        ));
    }
    let document: serde_json::Value = serde_json::from_slice(&manifest).map_err(|error| {
        invalid_release(format!("re-seal work inventory is malformed: {error}"))
    })?;
    let files = document
        .get("files")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| invalid_release("re-seal work inventory files are missing"))?;
    let mut inventory_bytes = 0_u64;
    for entry in files {
        let relative = entry
            .get("path")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| invalid_release("re-seal work inventory path is missing"))?;
        let size = entry
            .get("size")
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| invalid_release("re-seal work inventory size is missing"))?;
        let path = sealed_root.join(safe_release_relative_path(
            relative,
            "re-seal work inventory file",
        )?);
        require_regular_file(&path, "re-seal work inventory file")?;
        let metadata = fs::metadata(&path)?;
        reject_release_hardlink(&metadata, &path)?;
        if metadata.len() != size {
            return Err(invalid_release(format!(
                "re-seal work inventory size drift: {relative}"
            )));
        }
        inventory_bytes = inventory_bytes
            .checked_add(size)
            .ok_or_else(|| invalid_release("re-seal work inventory byte count overflow"))?;
    }
    let file_count = u64::try_from(files.len())
        .map_err(|_| invalid_release("re-seal work inventory file count overflow"))?;
    packaging_toolchain::VerifiedOutputBudget::sealed_python_reseal(inventory_bytes, file_count)
}

fn bind_sealed_python_root(root: &Path, require_formal_binding: bool) -> io::Result<()> {
    reject_unsupported_sealed_python_release_target()?;
    let manifest_path = root.join(SEALED_PYTHON_MANIFEST);
    if !manifest_path.exists() {
        println!("cargo:rustc-env=TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256=");
        if required_cargo_profile()? == "release" {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                format!("release packaging requires {SEALED_PYTHON_ROOT}/{SEALED_PYTHON_MANIFEST}"),
            ));
        }
        return Ok(());
    }
    require_directory(&root, "sealed Python environment root")?;
    require_regular_file(&manifest_path, "sealed Python environment manifest")?;
    let bytes = fs::read(&manifest_path)?;
    if required_cargo_profile()? == "release" && require_formal_binding {
        let expected = std::env::var(PACKAGING_PYTHON_INVENTORY_SHA_ENV).map_err(|_| {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!("{PACKAGING_PYTHON_INVENTORY_SHA_ENV} is required"),
            )
        })?;
        if !valid_raw_sha256(&expected) || raw_byte_digest(&bytes) != expected {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "sealed Python manifest differs from the formal inventory binding",
            ));
        }
    }
    let value: serde_json::Value = serde_json::from_slice(&bytes).map_err(|error| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("sealed Python manifest is malformed: {error}"),
        )
    })?;
    let object = value.as_object().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python manifest must be an object",
        )
    })?;
    let expected_fields = [
        "schema",
        "environment_digest",
        "platform",
        "architecture",
        "python_version",
        "package_provenance",
        "sentinels",
        "files",
    ]
    .into_iter()
    .collect::<std::collections::BTreeSet<_>>();
    let actual_fields = object
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    if actual_fields != expected_fields
        || object.get("schema").and_then(serde_json::Value::as_str) != Some(SEALED_PYTHON_SCHEMA)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python manifest schema or exact fields are invalid",
        ));
    }
    let target = required_cargo_target()?;
    let (platform, architecture) = expected_sealed_python_target(&target)?;
    if object.get("platform").and_then(serde_json::Value::as_str) != Some(platform)
        || object
            .get("architecture")
            .and_then(serde_json::Value::as_str)
            != Some(architecture)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python manifest platform/architecture differs from Cargo target",
        ));
    }
    let provenance = exact_object(
        object.get("package_provenance"),
        &["kind", "package_id", "release_digest"],
        "package_provenance",
    )?;
    let required_provenance = match platform {
        "macos" => "pinned-python-build-standalone-v1",
        "windows" => "windows-authenticode-v1",
        _ => "linux-immutable-package-v1",
    };
    if provenance.get("kind").and_then(serde_json::Value::as_str) != Some(required_provenance)
        || provenance
            .get("package_id")
            .and_then(serde_json::Value::as_str)
            != Some("dev.rumiai.app")
        || !valid_sha256(provenance.get("release_digest"))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python package provenance is invalid",
        ));
    }
    let sentinels = exact_object(
        object.get("sentinels"),
        &["stdlib_sha256", "site_packages_sha256", "native_sha256"],
        "sentinels",
    )?;
    if sentinels.values().any(|value| !valid_sha256(Some(value))) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python sentinel identity is invalid",
        ));
    }
    let files = object
        .get("files")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "sealed Python files must be an array",
            )
        })?;
    let environment_digest = object
        .get("environment_digest")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "environment_digest missing"))?;
    if sealed_python_inventory_digest(files)? != environment_digest {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python environment digest differs from sorted file inventory",
        ));
    }
    let mut expected_paths = Vec::new();
    for entry in files {
        let entry = entry.as_object().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "sealed Python file entry must be an object",
            )
        })?;
        let fields = entry
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>();
        if fields
            != ["path", "size", "sha256", "executable"]
                .into_iter()
                .collect()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "sealed Python file entry exact fields are invalid",
            ));
        }
        let relative = entry
            .get("path")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "sealed Python file path missing",
                )
            })?;
        let relative_path = safe_release_relative_path(relative, "sealed Python file")?;
        let path = root.join(&relative_path);
        require_regular_file(&path, "sealed Python inventory file")?;
        reject_release_hardlink(&fs::metadata(&path)?, &path)?;
        let payload = fs::read(&path)?;
        if entry.get("size").and_then(serde_json::Value::as_u64) != Some(payload.len() as u64)
            || entry.get("sha256").and_then(serde_json::Value::as_str)
                != Some(raw_byte_digest(&payload).as_str())
            || entry
                .get("executable")
                .and_then(serde_json::Value::as_bool)
                .is_none()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("sealed Python file identity drift: {relative}"),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let metadata = fs::metadata(&path)?;
            let executable = metadata.permissions().mode() & 0o111 != 0;
            let expected_mode = if executable { 0o555 } else { 0o444 };
            if entry.get("executable").and_then(serde_json::Value::as_bool) != Some(executable)
                || metadata.permissions().mode() & 0o777 != expected_mode
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("sealed Python file permissions drift: {relative}"),
                ));
            }
        }
        expected_paths.push(relative.to_string());
    }
    if !expected_paths.windows(2).all(|pair| pair[0] < pair[1]) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python file inventory must be strictly sorted and unique",
        ));
    }
    let required_interpreter = if platform == "windows" {
        "venv/Scripts/python.exe"
    } else {
        "venv/bin/python3"
    };
    let python_version = object
        .get("python_version")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "python_version missing"))?;
    let version = python_version.split('.').collect::<Vec<_>>();
    if version.len() != 3
        || version
            .iter()
            .any(|part| part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "python_version must be an exact numeric patch version",
        ));
    }
    let bootstrap = if platform == "windows" {
        "venv/Lib/site-packages/tobkiri_sealed/bootstrap.py".to_string()
    } else {
        format!(
            "venv/lib/python{}.{}/site-packages/tobkiri_sealed/bootstrap.py",
            version[0], version[1]
        )
    };
    let mut required_paths = vec![
        required_interpreter,
        "app/kernel_entry.py",
        "app/defaultspack_entry.py",
        "app/host_helper_entry.py",
        "sentinels/stdlib.sha256",
        "sentinels/site-packages.sha256",
        "sentinels/native.sha256",
        "lease.v1",
    ];
    required_paths.push(&bootstrap);
    for required in required_paths {
        if expected_paths
            .binary_search_by(|path| path.as_str().cmp(required))
            .is_err()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("sealed Python fixed layout is missing {required}"),
            ));
        }
    }
    let bootstrap_bytes = fs::read(root.join(&bootstrap))?;
    let bootstrap_text = std::str::from_utf8(&bootstrap_bytes).map_err(|error| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("sealed Python bootstrap is not UTF-8: {error}"),
        )
    })?;
    sealed_python_protocol::validate_bootstrap_template(bootstrap_text).map_err(|error| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("sealed Python bootstrap wire contract rejected: {error}"),
        )
    })?;
    if files
        .iter()
        .find(|entry| {
            entry.get("path").and_then(serde_json::Value::as_str) == Some(required_interpreter)
        })
        .and_then(|entry| entry.get("executable"))
        .and_then(serde_json::Value::as_bool)
        != Some(true)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python fixed interpreter must be executable",
        ));
    }
    let mut actual_paths = collect_runtime_resource_files(&root, &root)?
        .into_iter()
        .filter(|path| path != Path::new(SEALED_PYTHON_MANIFEST))
        .map(|path| portable_relative_path(&path))
        .collect::<Vec<_>>();
    actual_paths.sort();
    if actual_paths != expected_paths {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python environment contains missing or extra files",
        ));
    }
    if expected_paths
        .binary_search_by(|path| path.as_str().cmp(SEALED_PYTHON_DIRECTORY_MODES))
        .is_err()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python directory mode evidence is missing",
        ));
    }
    verify_sealed_python_directory_modes(root, &expected_paths)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::symlink_metadata(&manifest_path)?.permissions().mode() & 0o777 != 0o444 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "sealed Python manifest permissions drift",
            ));
        }
    }
    println!(
        "cargo:rustc-env=TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256={}",
        raw_byte_digest(&bytes)
    );
    Ok(())
}

fn verify_sealed_python_directory_modes(root: &Path, files: &[String]) -> io::Result<()> {
    let mut expected_directories = std::collections::BTreeSet::new();
    for relative in files {
        let mut parent = Path::new(relative).parent();
        while let Some(directory) = parent.filter(|path| !path.as_os_str().is_empty()) {
            expected_directories.insert(portable_relative_path(directory));
            parent = directory.parent();
        }
    }
    let actual_directories = collect_runtime_resource_directories(root, root)?;
    if actual_directories != expected_directories.iter().cloned().collect::<Vec<_>>() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python directory inventory drift",
        ));
    }
    let expected_entries = std::iter::once(serde_json::json!({
        "path": ".",
        "mode": "0555",
    }))
    .chain(
        expected_directories
            .iter()
            .map(|path| serde_json::json!({"path": path, "mode": "0555"})),
    )
    .collect::<Vec<_>>();
    let evidence_path = root.join(SEALED_PYTHON_DIRECTORY_MODES);
    require_regular_file(&evidence_path, "sealed Python directory mode evidence")?;
    let evidence: serde_json::Value = serde_json::from_slice(&fs::read(&evidence_path)?)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if evidence
        != serde_json::json!({
            "schema": SEALED_PYTHON_DIRECTORY_MODES_SCHEMA,
            "directories": expected_entries,
        })
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python directory mode evidence is invalid",
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for relative in std::iter::once(".").chain(expected_directories.iter().map(String::as_str))
        {
            let path = if relative == "." {
                root.to_path_buf()
            } else {
                root.join(relative)
            };
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.is_dir()
                || metadata.file_type().is_symlink()
                || metadata.permissions().mode() & 0o777 != 0o555
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("sealed Python directory permissions drift: {relative}"),
                ));
            }
        }
    }
    Ok(())
}

fn sealed_python_inventory_digest(files: &[serde_json::Value]) -> io::Result<String> {
    let mut payload = String::from("[");
    for (index, value) in files.iter().enumerate() {
        let entry = value.as_object().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "sealed Python file entry must be an object",
            )
        })?;
        if index != 0 {
            payload.push(',');
        }
        payload.push_str("{\"path\":");
        payload.push_str(&serde_json::to_string(entry.get("path").ok_or_else(
            || io::Error::new(io::ErrorKind::InvalidData, "sealed Python path missing"),
        )?)?);
        payload.push_str(",\"size\":");
        payload.push_str(&serde_json::to_string(entry.get("size").ok_or_else(
            || io::Error::new(io::ErrorKind::InvalidData, "sealed Python size missing"),
        )?)?);
        payload.push_str(",\"sha256\":");
        payload.push_str(&serde_json::to_string(entry.get("sha256").ok_or_else(
            || io::Error::new(io::ErrorKind::InvalidData, "sealed Python digest missing"),
        )?)?);
        payload.push_str(",\"executable\":");
        payload.push_str(&serde_json::to_string(
            entry.get("executable").ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "sealed Python executable flag missing",
                )
            })?,
        )?);
        payload.push('}');
    }
    payload.push(']');
    Ok(raw_byte_digest(payload.as_bytes()))
}

fn reject_unsupported_sealed_python_release_target() -> io::Result<()> {
    if required_cargo_profile()? != "release" {
        return Ok(());
    }
    let target = required_cargo_target()?;
    if !target.contains("apple-darwin") {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            format!(
                "release packaging is disabled for {target}: sealed Python package provenance is not implemented"
            ),
        ));
    }
    Ok(())
}

fn exact_object<'a>(
    value: Option<&'a serde_json::Value>,
    fields: &[&str],
    label: &str,
) -> io::Result<&'a serde_json::Map<String, serde_json::Value>> {
    let object = value
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{label} must be an object"),
            )
        })?;
    let actual = object
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>();
    let expected = fields
        .iter()
        .copied()
        .collect::<std::collections::BTreeSet<_>>();
    if actual != expected {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{label} exact fields are invalid"),
        ));
    }
    Ok(object)
}

fn valid_sha256(value: Option<&serde_json::Value>) -> bool {
    value
        .and_then(serde_json::Value::as_str)
        .is_some_and(|value| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
}

fn valid_raw_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn raw_byte_digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn hex_bytes(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

fn portable_relative_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn canonical_host_files(source_root: &Path) -> io::Result<Vec<String>> {
    let inventory = source_root
        .join("tobkiri_host")
        .join(CANONICAL_HOST_INVENTORY);
    let metadata = fs::symlink_metadata(&inventory)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "canonical Host inventory is missing or unsafe",
        ));
    }
    let document: serde_json::Value = serde_json::from_slice(&fs::read(&inventory)?)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let object = document.as_object().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "canonical Host inventory must be an object",
        )
    })?;
    let mut keys = object.keys().map(String::as_str).collect::<Vec<_>>();
    keys.sort_unstable();
    if keys != ["files", "schema"]
        || object.get("schema").and_then(serde_json::Value::as_str)
            != Some(CANONICAL_HOST_INVENTORY_SCHEMA)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "canonical Host inventory shape or schema is invalid",
        ));
    }
    let files = object
        .get("files")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "canonical Host inventory files must be an array",
            )
        })?
        .iter()
        .map(|value| {
            value.as_str().map(str::to_owned).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "canonical Host inventory filenames must be strings",
                )
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    let mut sorted = files.clone();
    sorted.sort();
    sorted.dedup();
    if files.is_empty()
        || files != sorted
        || !files.iter().any(|name| name == CANONICAL_HOST_INVENTORY)
        || files.iter().any(|name| {
            Path::new(name).components().count() != 1
                || matches!(
                    Path::new(name).components().next(),
                    Some(Component::CurDir | Component::ParentDir | Component::RootDir)
                )
        })
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "canonical Host inventory must be safe, sorted, unique, and self-listed",
        ));
    }
    let host_root = source_root.join("tobkiri_host");
    let mut actual_source_files = Vec::new();
    for entry in fs::read_dir(&host_root)? {
        let entry = entry?;
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "canonical Host source is a symlink: {}",
                    entry.path().display()
                ),
            ));
        }
        if metadata.is_file() {
            actual_source_files.push(entry.file_name().to_string_lossy().into_owned());
        }
    }
    actual_source_files.sort();
    if actual_source_files != files {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "canonical Host source inventory mismatch",
        ));
    }
    Ok(files)
}

fn verify_canonical_host_package(staged_root: &Path, source_root: &Path) -> io::Result<()> {
    let host_root = staged_root.join("tobkiri_host");
    let source_host_root = source_root.join("tobkiri_host");
    let expected = canonical_host_files(source_root)?;
    let mut actual = Vec::new();
    for entry in fs::read_dir(&host_root)? {
        let entry = entry?;
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("unsafe canonical Host resource: {}", entry.path().display()),
            ));
        }
        actual.push(entry.file_name().to_string_lossy().into_owned());
    }
    actual.sort();
    if actual != expected {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "canonical Host resource inventory mismatch",
        ));
    }
    for filename in expected {
        let source = source_host_root.join(&filename);
        let staged = host_root.join(&filename);
        let source_metadata = fs::symlink_metadata(&source)?;
        if source_metadata.file_type().is_symlink() || !source_metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("unsafe canonical Host source: {}", source.display()),
            ));
        }
        if fs::read(&source)? != fs::read(&staged)? {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("canonical Host resource byte mismatch: {filename}"),
            ));
        }
    }
    Ok(())
}

fn write_runtime_resource_manifest(staged_root: &Path) -> io::Result<()> {
    let mut ambiguity_keys = std::collections::BTreeSet::new();
    let entries = collect_runtime_resource_files(staged_root, staged_root)?
        .into_iter()
        .map(|relative| {
            let portable = portable_relative_path(&relative);
            let canonical = runtime_resource_paths::CanonicalResourcePath::parse(&portable)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
            if let Some(application) = canonical.as_str().strip_prefix("python-runtime/app/") {
                let sealed = format!("app/{application}");
                let binding =
                    runtime_resource_paths::SealedApplicationResourceBinding::from_sealed_path(
                        &sealed,
                    )
                    .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
                if binding.outer != canonical {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "sealed application resource domain is inconsistent",
                    ));
                }
            }
            if !ambiguity_keys.insert(canonical.ambiguity_key()) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "runtime resource paths are ambiguous by ASCII case",
                ));
            }
            let payload = fs::read(staged_root.join(&relative))?;
            Ok(serde_json::json!({
                "path": canonical.as_str(),
                "size": payload.len(),
                "sha256": format!("{:x}", Sha256::digest(&payload)),
            }))
        })
        .collect::<io::Result<Vec<_>>>()?;
    let document = serde_json::json!({
        "schema": RUNTIME_RESOURCE_SCHEMA,
        "entries": entries,
    });
    let payload = serde_json::to_vec_pretty(&document)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    fs::write(
        staged_root.join(RUNTIME_RESOURCE_MANIFEST),
        [payload, b"\n".to_vec()].concat(),
    )
}

#[derive(Debug)]
struct VerifiedPresentationRelease {
    public_key: String,
    key_id: String,
    artifact_path: PathBuf,
    artifact_ref: String,
    entrypoint: String,
    bundle_identity: String,
    platform: String,
    architecture: String,
    default_profile_sha256: String,
    defaultspack_lock_sha256: String,
}

fn invalid_release(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn object_field<'a>(
    object: &'a serde_json::Map<String, serde_json::Value>,
    field: &str,
    label: &str,
) -> io::Result<&'a serde_json::Value> {
    object
        .get(field)
        .ok_or_else(|| invalid_release(format!("{label} is missing field {field}")))
}

fn text_field(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    label: &str,
) -> io::Result<String> {
    object_field(object, field, label)?
        .as_str()
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .ok_or_else(|| invalid_release(format!("{label} field {field} is not non-empty text")))
}

fn digest_field(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    label: &str,
) -> io::Result<String> {
    let value = text_field(object, field, label)?;
    if value.len() != 71
        || !value.starts_with("sha256:")
        || !value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(invalid_release(format!(
            "{label} field {field} is not a canonical sha256 digest"
        )));
    }
    Ok(value)
}

fn byte_digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn canonical_value_digest(value: &serde_json::Value, label: &str) -> io::Result<String> {
    let bytes = serde_json::to_vec(value)
        .map_err(|error| invalid_release(format!("{label} cannot be canonicalized: {error}")))?;
    Ok(byte_digest(&bytes))
}

fn safe_release_relative_path(value: &str, label: &str) -> io::Result<PathBuf> {
    let path = Path::new(value);
    if value.is_empty()
        || value.starts_with('~')
        || value.contains('\\')
        || path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::ParentDir | Component::RootDir))
    {
        return Err(invalid_release(format!("{label} is unsafe: {value}")));
    }
    Ok(path.to_path_buf())
}

fn require_release_path(root: &Path, relative: &str, label: &str) -> io::Result<PathBuf> {
    let relative_path = safe_release_relative_path(relative, label)?;
    let candidate = root.join(&relative_path);
    let mut current = root.to_path_buf();
    for component in relative_path.components() {
        let Component::Normal(part) = component else {
            return Err(invalid_release(format!("{label} is unsafe: {relative}")));
        };
        current.push(part);
        if fs::symlink_metadata(&current)
            .map(|metadata| metadata.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(invalid_release(format!(
                "{label} contains a symlink: {}",
                current.display()
            )));
        }
    }
    let root_resolved = root.canonicalize().map_err(|error| {
        invalid_release(format!("{label} release root cannot be resolved: {error}"))
    })?;
    let candidate_resolved = candidate.canonicalize().map_err(|error| {
        invalid_release(format!(
            "{label} is missing at {}: {error}",
            candidate.display()
        ))
    })?;
    if !candidate_resolved.starts_with(&root_resolved) {
        return Err(invalid_release(format!(
            "{label} escapes the release root: {relative}"
        )));
    }
    Ok(candidate)
}

fn release_artifact_digest(path: &Path) -> io::Result<(String, u64)> {
    artifact_integrity::digest_and_size(path)
}

fn release_entrypoint(artifact: &Path, entrypoint: &str) -> io::Result<PathBuf> {
    let relative = safe_release_relative_path(entrypoint, "artifact entrypoint")?;
    let candidate = if artifact.is_dir()
        && relative.components().next().and_then(|part| match part {
            Component::Normal(value) => Some(value),
            _ => None,
        }) == artifact.file_name()
    {
        artifact.join(relative.components().skip(1).collect::<PathBuf>())
    } else if artifact.is_dir() {
        artifact.join(relative)
    } else {
        artifact.to_path_buf()
    };
    require_regular_file(&candidate, "release artifact entrypoint")?;
    let artifact_parent = if artifact.is_dir() {
        artifact.canonicalize()?
    } else {
        artifact.parent().unwrap_or(artifact).canonicalize()?
    };
    if !candidate.canonicalize()?.starts_with(artifact_parent) {
        return Err(invalid_release(
            "release artifact entrypoint escapes its artifact",
        ));
    }
    Ok(candidate)
}

fn git_value(repo_root: &Path, args: &[&str], label: &str) -> io::Result<String> {
    let git = packaging_toolchain::verified_tool("git")?;
    let output = git
        .command()?
        .args(args)
        .current_dir(repo_root)?
        .output()
        .map_err(|error| invalid_release(format!("failed to read {label}: {error}")))?;
    if !output.status.success() {
        return Err(invalid_release(format!(
            "failed to read {label}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    let value = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if value.is_empty() {
        return Err(invalid_release(format!("Git returned an empty {label}")));
    }
    Ok(value)
}

fn source_identity_from_remote(remote: &str) -> String {
    let without_suffix = remote.trim_end_matches('/').trim_end_matches(".git");
    for prefix in [
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
    ] {
        if let Some(repository) = without_suffix.strip_prefix(prefix) {
            return format!("github:{repository}");
        }
    }
    format!("git:{remote}")
}

#[derive(serde::Deserialize)]
struct VersionedSourceAuthority {
    schema: String,
    status: String,
    repository: String,
}

fn source_provenance_from_blob(blob: &[u8], revision: &str) -> io::Result<(String, String)> {
    if revision.len() != 40
        || !revision
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(invalid_release(format!(
            "source revision is not a full lowercase commit SHA: {revision}"
        )));
    }
    let authority: VersionedSourceAuthority = serde_json::from_slice(blob).map_err(|error| {
        invalid_release(format!(
            "versioned source authority is invalid JSON: {error}"
        ))
    })?;
    if authority.schema != "io.tobkiri.architecture.design-inputs.v1"
        || authority.status != "normative-provenance"
    {
        return Err(invalid_release(
            "versioned source authority schema/status is invalid",
        ));
    }
    let upstream = source_identity_from_remote(&authority.repository);
    if !upstream.starts_with("github:")
        || upstream.contains(char::is_whitespace)
        || upstream.contains(['?', '#'])
    {
        return Err(invalid_release(
            "versioned source authority repository is not canonical GitHub identity",
        ));
    }
    let authority_digest = format!("{:x}", Sha256::digest(blob));
    Ok((
        format!("{upstream}@sha256:{authority_digest}"),
        revision.to_owned(),
    ))
}

fn current_source_provenance() -> io::Result<(String, String)> {
    let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = project_dir
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| invalid_release("src-tauri has no repository root"))?;
    let revision = git_value(
        repo_root,
        &["rev-parse", "--verify", "HEAD"],
        "source revision",
    )?;
    let git = packaging_toolchain::verified_tool("git")?;
    let object = format!("{revision}:{SOURCE_AUTHORITY_PATH}");
    let authority = git
        .command()?
        .args(["show", &object])
        .current_dir(repo_root)?
        .output()
        .map_err(|error| invalid_release(format!("failed to read source authority: {error}")))?;
    if !authority.status.success() {
        return Err(invalid_release(
            "versioned source authority is absent from the source commit",
        ));
    }
    source_provenance_from_blob(&authority.stdout, &revision)
}

fn expected_target() -> io::Result<(String, String)> {
    let target = std::env::var("TARGET")
        .map_err(|_| invalid_release("Cargo TARGET is missing for a release package"))?;
    let value = match target.as_str() {
        "aarch64-apple-darwin" => ("macos", "arm64"),
        "x86_64-apple-darwin" => ("macos", "x86_64"),
        "x86_64-pc-windows-msvc" => ("windows", "x86_64"),
        "x86_64-unknown-linux-gnu" => ("linux", "x86_64"),
        _ => {
            return Err(invalid_release(format!(
                "unsupported release target: {target}"
            )))
        }
    };
    Ok((value.0.to_string(), value.1.to_string()))
}

fn verify_presentation_release(release_root: &Path) -> io::Result<VerifiedPresentationRelease> {
    verify_presentation_release_at(
        release_root,
        &release_root.join(PRESENTATION_CATALOG_FILENAME),
    )
}

fn verify_presentation_release_at(
    release_root: &Path,
    catalog_path: &Path,
) -> io::Result<VerifiedPresentationRelease> {
    require_directory(release_root, "release presentation root")?;
    let index_path = release_root
        .join("bundled")
        .join(PRESENTATION_INDEX_FILENAME);
    let lock_path = release_root
        .join("bundled")
        .join(PRESENTATION_LOCK_FILENAME);
    let release_path = release_root
        .join("bundled")
        .join(PRESENTATION_RELEASE_FILENAME);
    let catalog_raw = read_regular_file(catalog_path, "release presentation catalog")?;
    let index_raw = read_regular_file(&index_path, "release presentation artifact index")?;
    let lock_raw = read_regular_file(&lock_path, "release presentation profile lock")?;
    let release_raw = read_regular_file(&release_path, "release presentation manifest")?;
    let catalog = serde_json::from_slice::<serde_json::Value>(&catalog_raw)
        .map_err(|error| invalid_release(format!("presentation catalog is malformed: {error}")))?;
    let index = serde_json::from_slice::<serde_json::Value>(&index_raw)
        .map_err(|error| invalid_release(format!("artifact index is malformed: {error}")))?;
    let lock = serde_json::from_slice::<serde_json::Value>(&lock_raw)
        .map_err(|error| invalid_release(format!("profile lock is malformed: {error}")))?;
    let release = serde_json::from_slice::<serde_json::Value>(&release_raw)
        .map_err(|error| invalid_release(format!("release manifest is malformed: {error}")))?;
    let catalog_object = catalog
        .as_object()
        .ok_or_else(|| invalid_release("presentation catalog must be an object"))?;
    let index_object = index
        .as_object()
        .ok_or_else(|| invalid_release("artifact index must be an object"))?;
    let lock_object = lock
        .as_object()
        .ok_or_else(|| invalid_release("profile lock must be an object"))?;
    let release_object = release
        .as_object()
        .ok_or_else(|| invalid_release("release manifest must be an object"))?;
    let release_fields = [
        "schema",
        "catalog_path",
        "catalog_sha256",
        "artifact_index_path",
        "artifact_index_sha256",
        "profile_lock_path",
        "profile_lock_sha256",
        "default_profile_path",
        "default_profile_sha256",
        "defaultspack_lock_path",
        "defaultspack_lock_sha256",
        "artifact_id",
        "platform",
        "architecture",
        "source_identity",
        "source_revision",
        "key_id",
        "public_key",
        "signature",
    ];
    if release_object.len() != release_fields.len()
        || release_fields
            .iter()
            .any(|field| !release_object.contains_key(*field))
    {
        return Err(invalid_release(
            "release manifest has unknown or missing fields",
        ));
    }

    if text_field(catalog_object, "schema", "presentation catalog")? != PRESENTATION_CATALOG_SCHEMA
    {
        return Err(invalid_release("presentation catalog schema is invalid"));
    }
    if text_field(release_object, "schema", "release manifest")? != PRESENTATION_RELEASE_SCHEMA
        || text_field(index_object, "schema", "artifact index")? != PRESENTATION_INDEX_SCHEMA
        || text_field(lock_object, "schema", "profile lock")? != PRESENTATION_LOCK_SCHEMA
    {
        return Err(invalid_release("v4 release binding schema is invalid"));
    }

    let release_catalog_path = text_field(release_object, "catalog_path", "release manifest")?;
    let release_index_path = text_field(release_object, "artifact_index_path", "release manifest")?;
    let release_lock_path = text_field(release_object, "profile_lock_path", "release manifest")?;
    if release_catalog_path != "bundled/presentation_catalog.json"
        || release_index_path != "bundled/shell_artifact_index.v4.json"
        || release_lock_path != "bundled/shell_profile_lock.v4.json"
    {
        return Err(invalid_release(
            "release manifest uses non-canonical v4 paths",
        ));
    }
    if text_field(release_object, "default_profile_path", "release manifest")?
        != "ecosystem/defaultspack/v4/defaults.profile.v4.json"
        || text_field(release_object, "defaultspack_lock_path", "release manifest")?
            != "ecosystem/defaultspack/v4/bundle.lock.json"
    {
        return Err(invalid_release(
            "release manifest uses non-canonical packaged Defaults paths",
        ));
    }
    let catalog_digest = digest_field(release_object, "catalog_sha256", "release manifest")?;
    let index_file_digest =
        digest_field(release_object, "artifact_index_sha256", "release manifest")?;
    let lock_file_digest = digest_field(release_object, "profile_lock_sha256", "release manifest")?;
    let default_profile_sha256 =
        digest_field(release_object, "default_profile_sha256", "release manifest")?;
    let defaultspack_lock_sha256 = digest_field(
        release_object,
        "defaultspack_lock_sha256",
        "release manifest",
    )?;
    let release_profile = require_release_path(
        release_root,
        "ecosystem/defaultspack/v4/defaults.profile.v4.json",
        "release default Profile",
    )?;
    let release_defaultspack_lock = require_release_path(
        release_root,
        "ecosystem/defaultspack/v4/bundle.lock.json",
        "release Defaults lock",
    )?;
    if catalog_digest != byte_digest(&catalog_raw)
        || index_file_digest != byte_digest(&index_raw)
        || lock_file_digest != byte_digest(&lock_raw)
    {
        return Err(invalid_release("release manifest byte digest mismatch"));
    }
    if digest_field(
        catalog_object,
        "default_profile_digest",
        "presentation catalog",
    )? != default_profile_sha256
    {
        return Err(invalid_release(
            "presentation catalog Profile identity differs from release manifest",
        ));
    }
    if byte_digest(&fs::read(release_profile)?) != default_profile_sha256
        || byte_digest(&fs::read(&release_defaultspack_lock)?) != defaultspack_lock_sha256
    {
        return Err(invalid_release(
            "release packaged Defaults bytes differ from signed identities",
        ));
    }
    verify_catalog_source_manifest_digests(&catalog, &release_defaultspack_lock)?;

    let binding = object_field(catalog_object, "release_binding", "presentation catalog")?
        .as_object()
        .ok_or_else(|| invalid_release("production catalog has no v4 release binding"))?;
    if text_field(binding, "schema", "catalog release binding")? != PRESENTATION_RELEASE_SCHEMA
        || text_field(binding, "artifact_index_path", "catalog release binding")?
            != "bundled/shell_artifact_index.v4.json"
        || text_field(binding, "profile_lock_path", "catalog release binding")?
            != "bundled/shell_profile_lock.v4.json"
    {
        return Err(invalid_release("catalog release binding is not canonical"));
    }
    let index_digest = digest_field(binding, "artifact_index_sha256", "catalog release binding")?;
    let lock_digest = digest_field(binding, "profile_lock_sha256", "catalog release binding")?;
    if index_digest != canonical_value_digest(&index, "artifact index")?
        || lock_digest != canonical_value_digest(&lock, "profile lock")?
    {
        return Err(invalid_release(
            "catalog v4 binding does not match index or lock",
        ));
    }
    let mut catalog_without_binding = catalog.clone();
    catalog_without_binding
        .as_object_mut()
        .expect("catalog object was checked above")
        .remove("release_binding");
    if text_field(binding, "catalog_revision", "catalog release binding")?
        != canonical_value_digest(&catalog_without_binding, "catalog")?
    {
        return Err(invalid_release("catalog revision mismatch"));
    }

    let mut lock_without_revision = lock.clone();
    lock_without_revision
        .as_object_mut()
        .expect("lock object was checked above")
        .remove("lock_revision");
    if text_field(lock_object, "lock_revision", "profile lock")?
        != canonical_value_digest(&lock_without_revision, "profile lock")?
    {
        return Err(invalid_release("profile lock revision mismatch"));
    }

    let artifact_id = text_field(release_object, "artifact_id", "release manifest")?;
    let platform = text_field(release_object, "platform", "release manifest")?;
    let architecture = text_field(release_object, "architecture", "release manifest")?;
    let source_identity = text_field(release_object, "source_identity", "release manifest")?;
    let source_revision = text_field(release_object, "source_revision", "release manifest")?;
    for field in [
        "artifact_id",
        "platform",
        "architecture",
        "source_identity",
        "source_revision",
    ] {
        let release_value = text_field(release_object, field, "release manifest")?;
        if text_field(binding, field, "catalog release binding")? != release_value
            || text_field(index_object, field, "artifact index")? != release_value
            || text_field(lock_object, field, "profile lock")? != release_value
        {
            return Err(invalid_release(format!(
                "v4 release field mismatch: {field}"
            )));
        }
    }
    #[cfg(not(test))]
    {
        let (expected_platform, expected_architecture) = expected_target()?;
        if (platform.as_str(), architecture.as_str())
            != (expected_platform.as_str(), expected_architecture.as_str())
        {
            return Err(invalid_release(
                "v4 release targets the wrong platform or architecture",
            ));
        }
    }

    let index_path_value = text_field(index_object, "path", "artifact index")?;
    let artifact_relative = safe_release_relative_path(&index_path_value, "artifact index path")?;
    if !index_path_value.starts_with("bundled/presentation-artifacts/") {
        return Err(invalid_release(
            "artifact index path is outside presentation-artifacts",
        ));
    }
    if text_field(binding, "artifact_id", "catalog release binding")? != artifact_id
        || text_field(index_object, "path", "artifact index")? != index_path_value
    {
        return Err(invalid_release("artifact identity/path binding mismatch"));
    }
    let artifact_path = require_release_path(
        release_root,
        artifact_relative.to_str().unwrap_or_default(),
        "release artifact",
    )?;
    let (artifact_digest, artifact_size) = release_artifact_digest(&artifact_path)?;
    if digest_field(index_object, "sha256", "artifact index")? != artifact_digest
        || digest_field(lock_object, "artifact_sha256", "profile lock")? != artifact_digest
        || object_field(index_object, "size", "artifact index")?.as_u64() != Some(artifact_size)
    {
        return Err(invalid_release("artifact digest or size mismatch"));
    }

    let default_selection =
        object_field(catalog_object, "default_selection", "presentation catalog")?
            .as_object()
            .ok_or_else(|| invalid_release("presentation catalog default selection is missing"))?;
    let shell_provider_id =
        text_field(default_selection, "shell_provider_id", "default selection")?;
    let shells = object_field(catalog_object, "shell_providers", "presentation catalog")?
        .as_array()
        .ok_or_else(|| invalid_release("presentation catalog Shell Providers are invalid"))?;
    let selected_shell = shells
        .iter()
        .filter_map(serde_json::Value::as_object)
        .find(|shell| {
            shell.get("provider_id").and_then(serde_json::Value::as_str)
                == Some(shell_provider_id.as_str())
        })
        .ok_or_else(|| invalid_release("default Profile Shell is missing"))?;
    let variants = object_field(selected_shell, "artifact_variants", "default Profile Shell")?
        .as_array()
        .ok_or_else(|| invalid_release("default Profile Shell artifact variants are invalid"))?;
    let selected_variant = variants
        .iter()
        .filter_map(serde_json::Value::as_object)
        .find(|variant| {
            variant
                .get("artifact_id")
                .and_then(serde_json::Value::as_str)
                == Some(artifact_id.as_str())
        })
        .ok_or_else(|| {
            invalid_release("signed artifact does not match the default Profile Shell")
        })?;
    for field in [
        "path",
        "sha256",
        "entrypoint_sha256",
        "source_identity",
        "source_revision",
    ] {
        if text_field(selected_variant, field, "selected artifact variant")?
            != text_field(index_object, field, "artifact index")?
        {
            return Err(invalid_release(format!(
                "catalog variant differs from artifact index: {field}"
            )));
        }
    }
    let entrypoint = text_field(selected_variant, "entrypoint", "selected artifact variant")?;
    let entrypoint_path = release_entrypoint(&artifact_path, &entrypoint)?;
    let entrypoint_digest = byte_digest(&fs::read(&entrypoint_path)?);
    if digest_field(index_object, "entrypoint_sha256", "artifact index")? != entrypoint_digest
        || digest_field(lock_object, "entrypoint_sha256", "profile lock")? != entrypoint_digest
    {
        return Err(invalid_release("artifact entrypoint digest mismatch"));
    }
    let artifact_ref = text_field(
        selected_variant,
        "artifact_ref",
        "selected artifact variant",
    )?;
    let bundle_identity = text_field(
        selected_variant,
        "bundle_identifier",
        "selected artifact variant",
    )?;
    if object_field(selected_variant, "size", "selected artifact variant")?.as_u64()
        != Some(artifact_size)
    {
        return Err(invalid_release(
            "catalog artifact size differs from artifact index",
        ));
    }
    if selected_variant
        .get("production")
        .and_then(serde_json::Value::as_bool)
        != Some(true)
        || selected_variant
            .get("prebuilt")
            .and_then(serde_json::Value::as_bool)
            != Some(true)
        || selected_variant
            .get("development_command")
            .and_then(serde_json::Value::as_str)
            .is_some_and(|value| !value.trim().is_empty())
    {
        return Err(invalid_release(
            "selected Shell artifact is not production-prebuilt",
        ));
    }
    if platform == "macos" {
        let bundle_identifier = text_field(
            selected_variant,
            "bundle_identifier",
            "selected artifact variant",
        )?;
        if artifact_path
            .extension()
            .and_then(|extension| extension.to_str())
            .is_none_or(|extension| extension != "app")
        {
            return Err(invalid_release(
                "macOS Shell artifact is not an .app bundle",
            ));
        }
        let plist_path = artifact_path.join("Contents").join("Info.plist");
        require_regular_file(&plist_path, "macOS Shell Info.plist")?;
        let output = Command::new("/usr/bin/plutil")
            .args([
                "-extract",
                "CFBundleIdentifier",
                "raw",
                "-o",
                "-",
                &plist_path.to_string_lossy(),
            ])
            .output()
            .map_err(|error| {
                invalid_release(format!("failed to read macOS bundle identity: {error}"))
            })?;
        if !output.status.success()
            || String::from_utf8_lossy(&output.stdout).trim() != bundle_identifier
        {
            return Err(invalid_release(
                "macOS Shell bundle identifier differs from the v4 catalog",
            ));
        }
    }

    let public_key = text_field(release_object, "public_key", "release manifest")?;
    let signature = text_field(release_object, "signature", "release manifest")?;
    let key_id = text_field(release_object, "key_id", "release manifest")?;
    let public_key_bytes: [u8; 32] = BASE64
        .decode(&public_key)
        .map_err(|error| invalid_release(format!("release public key is invalid: {error}")))?
        .try_into()
        .map_err(|_| invalid_release("release public key must be 32 bytes"))?;
    let signature_bytes: [u8; 64] = BASE64
        .decode(&signature)
        .map_err(|error| invalid_release(format!("release signature is invalid: {error}")))?
        .try_into()
        .map_err(|_| invalid_release("release signature must be 64 bytes"))?;
    let message = [
        PRESENTATION_RELEASE_SCHEMA,
        catalog_digest.as_str(),
        index_file_digest.as_str(),
        lock_file_digest.as_str(),
        default_profile_sha256.as_str(),
        defaultspack_lock_sha256.as_str(),
        source_identity.as_str(),
        source_revision.as_str(),
        platform.as_str(),
        architecture.as_str(),
        artifact_id.as_str(),
        key_id.as_str(),
    ]
    .join("\0");
    VerifyingKey::from_bytes(&public_key_bytes)
        .map_err(|error| invalid_release(format!("release public key is invalid: {error}")))?
        .verify(
            &message.into_bytes(),
            &Signature::from_bytes(&signature_bytes),
        )
        .map_err(|error| {
            invalid_release(format!("release signature verification failed: {error}"))
        })?;

    #[cfg(not(test))]
    {
        let (expected_identity, expected_revision) = current_source_provenance()?;
        if source_identity != expected_identity || source_revision != expected_revision {
            return Err(invalid_release(
                "v4 release source identity/revision is stale for this checkout",
            ));
        }
    }
    verify_release_artifact_scope(release_root, &artifact_path)?;
    Ok(VerifiedPresentationRelease {
        public_key,
        key_id,
        artifact_path,
        artifact_ref,
        entrypoint,
        bundle_identity,
        platform,
        architecture,
        default_profile_sha256,
        defaultspack_lock_sha256,
    })
}

fn is_intermediate_shell_build() -> bool {
    let Ok(raw_config) = std::env::var("TAURI_CONFIG") else {
        return false;
    };
    let Ok(config) = serde_json::from_str::<serde_json::Value>(&raw_config) else {
        return false;
    };
    config.get("identifier").and_then(serde_json::Value::as_str) == Some("io.tobkiri.shell.tauri")
        && config
            .get("mainBinaryName")
            .and_then(serde_json::Value::as_str)
            == Some("tobkiri-shell")
}

/// Whether this is the explicit debug-only local Launcher configuration.
///
/// It deliberately removes `gen/app` from the Tauri resource map. The debug
/// binary resolves its runtime from the local development workspace, so asking
/// it to create the production sealed runtime would both require the formal
/// packaging inputs and race the development resource preparation hook. The
/// distinct CI/E2E configuration retains `gen/app`; every release-profile
/// build always takes the sealed staging path.
fn is_unbundled_local_development_build() -> bool {
    if required_cargo_profile().ok().as_deref() != Some("debug") {
        return false;
    }
    let Ok(raw_config) = std::env::var("TAURI_CONFIG") else {
        return false;
    };
    let Ok(config) = serde_json::from_str::<serde_json::Value>(&raw_config) else {
        return false;
    };
    if config.get("identifier").and_then(serde_json::Value::as_str)
        != Some(LOCAL_DEVELOPMENT_LAUNCHER_IDENTIFIER)
    {
        return false;
    }
    config
        .get("bundle")
        .and_then(|bundle| bundle.get("resources"))
        .and_then(|resources| resources.get("./gen/app"))
        .map_or(true, serde_json::Value::is_null)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CoreBuildStage {
    IntermediateShell,
    FinalApplication,
}

fn core_build_stage() -> CoreBuildStage {
    if is_intermediate_shell_build() {
        CoreBuildStage::IntermediateShell
    } else {
        CoreBuildStage::FinalApplication
    }
}

struct ShellArtifactAuthority {
    path: PathBuf,
    artifact_id: String,
    artifact_ref: String,
    entrypoint: String,
    bundle_identity: String,
    platform: String,
    architecture: String,
}

#[cfg(target_os = "macos")]
struct CoreTransactionGuard {
    path: PathBuf,
    parent: File,
    root: File,
    name: std::ffi::OsString,
    identity: (u64, u64),
    inventory: Option<std::collections::BTreeMap<String, (u64, u64, bool)>>,
    armed: bool,
}

#[cfg(target_os = "macos")]
impl CoreTransactionGuard {
    fn create(parent: &Path) -> io::Result<Self> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::MetadataExt;
        let canonical_parent = parent.canonicalize()?;
        let parent_bytes = CString::new(canonical_parent.as_os_str().as_bytes())
            .map_err(|_| invalid_release("Core transaction parent contains NUL"))?;
        let parent_fd = unsafe {
            libc::open(
                parent_bytes.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if parent_fd == -1 {
            return Err(io::Error::last_os_error());
        }
        let parent = unsafe { File::from_raw_fd(parent_fd) };
        let mut nonce = [0_u8; 16];
        rand::rngs::OsRng.fill_bytes(&mut nonce);
        let name = format!(".tobkiri-core-presentation-{}", hex_bytes(&nonce));
        let encoded = CString::new(name.as_bytes())
            .map_err(|_| invalid_release("Core transaction name contains NUL"))?;
        if unsafe { libc::mkdirat(parent.as_raw_fd(), encoded.as_ptr(), 0o700) } == -1 {
            return Err(io::Error::last_os_error());
        }
        let root_fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                encoded.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if root_fd == -1 {
            return Err(invalid_release(format!(
                "{}; Core transaction residue retained because its identity is unavailable",
                io::Error::last_os_error()
            )));
        }
        let root = unsafe { File::from_raw_fd(root_fd) };
        let metadata = root.metadata().map_err(|error| {
            invalid_release(format!(
                "{error}; Core transaction residue retained because root identity is unavailable"
            ))
        })?;
        if metadata.mode() & 0o777 != 0o700 || metadata.uid() != unsafe { libc::geteuid() } {
            return Err(invalid_release(
                "Core transaction owner/mode is invalid; residue retained fail-closed",
            ));
        }
        Ok(Self {
            path: canonical_parent.join(&name),
            parent,
            root,
            name: name.into(),
            identity: (metadata.dev(), metadata.ino()),
            inventory: None,
            armed: true,
        })
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn seal_inventory(&mut self) -> io::Result<()> {
        self.inventory = Some(core_transaction_inventory(&self.root)?);
        Ok(())
    }

    fn cleanup(mut self) -> io::Result<()> {
        let inventory = self.inventory.as_ref().ok_or_else(|| {
            invalid_release(
                "Core transaction ownership is incomplete; residue retained fail-closed",
            )
        })?;
        verify_core_transaction_name(&self.parent, &self.name, self.identity)?;
        core_transaction_remove(&self.root, "", inventory)?;
        verify_core_transaction_name(&self.parent, &self.name, self.identity)?;
        unlinkat_core(&self.parent, &self.name, libc::AT_REMOVEDIR)?;
        self.armed = false;
        Ok(())
    }
}

#[cfg(target_os = "macos")]
impl Drop for CoreTransactionGuard {
    fn drop(&mut self) {
        // Explicit cleanup composes diagnostics. Drop never performs a path-based retry.
        if self.armed {}
    }
}

#[cfg(target_os = "macos")]
struct StagedRuntimeResetGuard {
    parent: File,
    root: File,
    name: std::ffi::OsString,
    identity: (u64, u64),
    inventory: std::collections::BTreeMap<String, (u64, u64, bool)>,
    armed: bool,
}

#[cfg(target_os = "macos")]
impl StagedRuntimeResetGuard {
    fn open(path: &Path) -> io::Result<Self> {
        use std::os::unix::fs::MetadataExt;

        reject_staged_path_components(path)?;
        let parent_path = path
            .parent()
            .ok_or_else(|| invalid_release("staged runtime root has no parent"))?;
        let parent = open_staged_directory(parent_path, "staged runtime parent")?;
        let name = path
            .file_name()
            .ok_or_else(|| invalid_release("staged runtime root has no name"))?
            .to_owned();
        let root = core_openat(&parent, &name, true)?;
        let metadata = root.metadata()?;
        if metadata.uid() != unsafe { libc::geteuid() } {
            return Err(invalid_release(
                "staged runtime root is not owned by the build host; residue retained",
            ));
        }
        let identity = (metadata.dev(), metadata.ino());
        let inventory = core_transaction_inventory(&root)?;
        if inventory.keys().any(|relative| !relative.is_empty()) {
            match core_openat(
                &root,
                std::ffi::OsStr::new(RUNTIME_RESOURCE_MANIFEST),
                false,
            ) {
                Ok(_) => validate_staged_runtime_manifest(&root, &inventory)?,
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    return Err(invalid_release(
                        "staged runtime seal manifest is missing; residue retained",
                    ));
                }
                Err(error) => return Err(error),
            }
        }
        Ok(Self {
            parent,
            root,
            name,
            identity,
            inventory,
            armed: true,
        })
    }

    fn cleanup(mut self) -> io::Result<()> {
        verify_core_transaction_name(&self.parent, &self.name, self.identity)?;
        core_transaction_remove(&self.root, "", &self.inventory)?;
        verify_core_transaction_name(&self.parent, &self.name, self.identity)?;
        unlinkat_core(&self.parent, &self.name, libc::AT_REMOVEDIR)?;
        self.armed = false;
        Ok(())
    }
}

#[cfg(target_os = "macos")]
impl Drop for StagedRuntimeResetGuard {
    fn drop(&mut self) {
        // Explicit cleanup is the only removal path.  Drop never retries by path.
        if self.armed {}
    }
}

#[cfg(target_os = "macos")]
fn reject_staged_path_components(path: &Path) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(invalid_release(
            "staged runtime path must be absolute; residue retained",
        ));
    }
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(invalid_release(
                    "staged runtime path contains a symlink; residue retained",
                ));
            }
            Ok(metadata) if !metadata.is_dir() => {
                return Err(invalid_release(
                    "staged runtime path contains a non-directory; residue retained",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn require_staged_owner(metadata: &fs::Metadata, label: &str) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    if metadata.uid() != unsafe { libc::geteuid() } {
        return Err(invalid_release(format!(
            "{label} is not owned by the build host; residue retained",
        )));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn open_staged_directory(path: &Path, label: &str) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::FromRawFd;
    use std::os::unix::ffi::OsStrExt;

    reject_staged_path_components(path)?;
    let encoded = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| invalid_release(format!("{label} contains NUL")))?;
    let fd = unsafe {
        libc::open(
            encoded.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd == -1 {
        return Err(io::Error::last_os_error());
    }
    let directory = unsafe { File::from_raw_fd(fd) };
    require_staged_owner(&directory.metadata()?, label)?;
    Ok(directory)
}

#[cfg(target_os = "macos")]
fn ensure_staged_parent(parent: &Path) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;

    reject_staged_path_components(parent)?;
    match fs::symlink_metadata(parent) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(invalid_release(
                    "staged runtime parent has an unsafe type; residue retained",
                ));
            }
            require_staged_owner(&metadata, "staged runtime parent")?;
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let grandparent = parent
                .parent()
                .ok_or_else(|| invalid_release("staged runtime parent has no parent"))?;
            let grandparent_metadata = fs::symlink_metadata(grandparent)?;
            if grandparent_metadata.file_type().is_symlink()
                || !grandparent_metadata.is_dir()
                || grandparent_metadata.uid() != unsafe { libc::geteuid() }
            {
                return Err(invalid_release(
                    "staged runtime parent anchor is not owned by the build host; residue retained",
                ));
            }
            fs::create_dir(parent)?;
            let metadata = fs::symlink_metadata(parent)?;
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(invalid_release(
                    "staged runtime parent was replaced during creation; residue retained",
                ));
            }
            require_staged_owner(&metadata, "staged runtime parent")?;
        }
        Err(error) => return Err(error),
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn create_staged_runtime_root(path: &Path) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;

    let parent_path = path
        .parent()
        .ok_or_else(|| invalid_release("staged runtime root has no parent"))?;
    let parent = open_staged_directory(parent_path, "staged runtime parent")?;
    let name = path
        .file_name()
        .ok_or_else(|| invalid_release("staged runtime root has no name"))?;
    let encoded = CString::new(name.as_bytes())
        .map_err(|_| invalid_release("staged runtime root name contains NUL"))?;
    if unsafe { libc::mkdirat(parent.as_raw_fd(), encoded.as_ptr(), 0o755) } == -1 {
        return Err(invalid_release(format!(
            "failed to create new staged runtime root: {}",
            io::Error::last_os_error()
        )));
    }
    let root = core_openat(&parent, name, true).map_err(|error| {
        invalid_release(format!(
            "new staged runtime root disappeared before it could be opened: {error}"
        ))
    })?;
    let metadata = root.metadata()?;
    if !metadata.is_dir() || metadata.uid() != unsafe { libc::geteuid() } {
        return Err(invalid_release(
            "new staged runtime root has unsafe ownership; residue retained",
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn reset_staged_runtime_macos(path: &Path) -> io::Result<()> {
    reject_staged_path_components(path)?;
    let parent = path
        .parent()
        .ok_or_else(|| invalid_release("staged runtime root has no parent"))?;
    ensure_staged_parent(parent)?;
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(invalid_release(
                    "staged runtime root has an unsafe type; residue retained",
                ));
            }
            require_staged_owner(&metadata, "staged runtime root")?;
            StagedRuntimeResetGuard::open(path)?.cleanup()?;
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    create_staged_runtime_root(path)
}

#[cfg(target_os = "macos")]
fn core_open_relative(root: &File, relative: &str, directory_only: bool) -> io::Result<File> {
    let relative_path = safe_release_relative_path(relative, "staged runtime entry")?;
    let components = relative_path
        .components()
        .map(|component| match component {
            Component::Normal(name) => Ok(name.to_owned()),
            _ => Err(invalid_release(
                "staged runtime entry contains a non-normal component",
            )),
        })
        .collect::<io::Result<Vec<_>>>()?;
    if components.is_empty() {
        return Err(invalid_release("staged runtime entry is empty"));
    }
    let mut directory = root.try_clone()?;
    for (index, name) in components.iter().enumerate() {
        let is_last = index + 1 == components.len();
        directory = core_openat(&directory, name, is_last && directory_only || !is_last)?;
    }
    Ok(directory)
}

#[cfg(target_os = "macos")]
fn core_create_directory(
    parent: &File,
    name: &std::ffi::OsStr,
    root_device: u64,
    label: &str,
) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;

    let encoded = CString::new(name.as_bytes())
        .map_err(|_| invalid_release(format!("{label} contains NUL")))?;
    if unsafe { libc::mkdirat(parent.as_raw_fd(), encoded.as_ptr(), 0o700) } == -1 {
        let error = io::Error::last_os_error();
        if error.kind() == io::ErrorKind::AlreadyExists {
            return Err(invalid_release(format!(
                "{label} already exists inside the Core transaction; residue retained"
            )));
        }
        return Err(error);
    }
    let directory = core_openat(parent, name, true)?;
    let metadata = directory.metadata()?;
    if !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.dev() != root_device
        || metadata.mode() & 0o777 != 0o700
    {
        return Err(invalid_release(format!(
            "{label} has unsafe Core transaction ownership; residue retained"
        )));
    }
    Ok(directory)
}

#[cfg(target_os = "macos")]
fn stage_core_defaults_bundle(
    transaction: &CoreTransactionGuard,
    repository_root: &Path,
) -> io::Result<PathBuf> {
    use std::os::unix::fs::MetadataExt;

    let source = repository_root
        .join(APP_SOURCE_DIR)
        .join("ecosystem/defaultspack/v4");
    require_directory(&source, "canonical Defaults v4 source bundle")?;
    let release = core_create_directory(
        &transaction.root,
        std::ffi::OsStr::new("release"),
        transaction.identity.0,
        "Core release root",
    )?;
    let ecosystem = core_create_directory(
        &release,
        std::ffi::OsStr::new("ecosystem"),
        transaction.identity.0,
        "Core release ecosystem root",
    )?;
    let defaultspack = core_create_directory(
        &ecosystem,
        std::ffi::OsStr::new("defaultspack"),
        transaction.identity.0,
        "Core release Defaultspack root",
    )?;
    let bundle_root = transaction.path.join("release/ecosystem/defaultspack/v4");
    copy_release_tree(&source, &bundle_root)?;
    mirror_directory_permissions(&source, &bundle_root)?;
    drop(defaultspack);
    let bound = core_open_relative(&transaction.root, "release/ecosystem/defaultspack/v4", true)?;
    let metadata = bound.metadata()?;
    if !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.dev() != transaction.identity.0
    {
        return Err(invalid_release(
            "Core Defaults bundle root identity changed; residue retained",
        ));
    }
    Ok(bundle_root)
}

#[cfg(target_os = "macos")]
fn validate_staged_runtime_manifest(
    root: &File,
    inventory: &std::collections::BTreeMap<String, (u64, u64, bool)>,
) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;

    let manifest = core_openat(root, std::ffi::OsStr::new(RUNTIME_RESOURCE_MANIFEST), false)?;
    let manifest_metadata = manifest.metadata()?;
    if !manifest_metadata.is_file()
        || manifest_metadata.nlink() != 1
        || manifest_metadata.uid() != unsafe { libc::geteuid() }
    {
        return Err(invalid_release(
            "staged runtime seal manifest has unsafe identity; residue retained",
        ));
    }
    let mut manifest_bytes = Vec::new();
    manifest
        .take(64 * 1024 * 1024)
        .read_to_end(&mut manifest_bytes)?;
    let document: serde_json::Value = serde_json::from_slice(&manifest_bytes).map_err(|error| {
        invalid_release(format!("staged runtime seal manifest is invalid: {error}"))
    })?;
    let object = exact_object(
        Some(&document),
        &["schema", "entries"],
        "staged runtime seal manifest",
    )?;
    if object.get("schema").and_then(serde_json::Value::as_str) != Some(RUNTIME_RESOURCE_SCHEMA) {
        return Err(invalid_release(
            "staged runtime seal manifest schema is invalid; residue retained",
        ));
    }
    let entries = object
        .get("entries")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| invalid_release("staged runtime seal entries are invalid"))?;
    if entries.is_empty() {
        return Err(invalid_release(
            "staged runtime seal has no entries; residue retained",
        ));
    }

    let mut expected = std::collections::BTreeMap::new();
    expected.insert(String::new(), true);
    expected.insert(RUNTIME_RESOURCE_MANIFEST.to_owned(), false);
    let mut verified_entries = Vec::with_capacity(entries.len());
    let mut previous_path = None;
    for value in entries {
        let entry = exact_object(
            Some(value),
            &["path", "size", "sha256"],
            "staged runtime seal entry",
        )?;
        let path_text = entry
            .get("path")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| invalid_release("staged runtime seal path is invalid"))?;
        let relative = safe_release_relative_path(path_text, "staged runtime seal path")?;
        if relative
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
            || portable_relative_path(&relative) != path_text
            || path_text == RUNTIME_RESOURCE_MANIFEST
        {
            return Err(invalid_release(
                "staged runtime seal path is not canonical; residue retained",
            ));
        }
        if previous_path
            .as_deref()
            .is_some_and(|previous| previous >= path_text)
        {
            return Err(invalid_release(
                "staged runtime seal entries are not canonical; residue retained",
            ));
        }
        previous_path = Some(path_text.to_owned());
        let size = entry
            .get("size")
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| invalid_release("staged runtime seal size is invalid"))?;
        let digest = entry
            .get("sha256")
            .and_then(serde_json::Value::as_str)
            .filter(|digest| valid_raw_sha256(digest))
            .ok_or_else(|| invalid_release("staged runtime seal digest is invalid"))?
            .to_owned();
        let normalized = portable_relative_path(&relative);
        if expected.insert(normalized.clone(), false).is_some() {
            return Err(invalid_release(
                "staged runtime seal contains a duplicate path; residue retained",
            ));
        }
        let mut directory = PathBuf::new();
        let components = relative.components().collect::<Vec<_>>();
        for component in components.iter().take(components.len() - 1) {
            let Component::Normal(name) = component else {
                return Err(invalid_release(
                    "staged runtime seal directory is not canonical; residue retained",
                ));
            };
            directory.push(name);
            let directory_text = portable_relative_path(&directory);
            if expected.get(&directory_text) == Some(&false) {
                return Err(invalid_release(
                    "staged runtime seal has a file/directory collision; residue retained",
                ));
            }
            expected.insert(directory_text, true);
        }
        verified_entries.push((normalized, size, digest));
    }

    if inventory.len() != expected.len()
        || expected.iter().any(|(path, expected_directory)| {
            inventory
                .get(path)
                .map(|(_, _, actual_directory)| actual_directory != expected_directory)
                != Some(false)
        })
    {
        return Err(invalid_release(
            "staged runtime seal does not cover the exact owned tree; residue retained",
        ));
    }

    for (path, (_, _, directory)) in inventory {
        let handle = if path.is_empty() {
            root.try_clone()?
        } else {
            core_open_relative(root, path, *directory)?
        };
        let metadata = handle.metadata()?;
        if metadata.uid() != unsafe { libc::geteuid() }
            || (*directory && !metadata.is_dir())
            || (!*directory && (!metadata.is_file() || metadata.nlink() != 1))
        {
            return Err(invalid_release(
                "staged runtime tree ownership or type is invalid; residue retained",
            ));
        }
    }

    for (path, expected_size, expected_digest) in verified_entries {
        let mut file = core_open_relative(root, &path, false)?;
        let before = file.metadata()?;
        let mut payload = Vec::new();
        file.read_to_end(&mut payload)?;
        let after = file.metadata()?;
        if before.dev() != after.dev()
            || before.ino() != after.ino()
            || before.len() != expected_size
            || payload.len() as u64 != expected_size
            || raw_byte_digest(&payload) != expected_digest
        {
            return Err(invalid_release(
                "staged runtime seal entry changed or has the wrong digest; residue retained",
            ));
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn core_openat(directory: &File, name: &std::ffi::OsStr, directory_only: bool) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    let encoded = CString::new(name.as_bytes())
        .map_err(|_| invalid_release("Core transaction entry contains NUL"))?;
    let mut flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    if directory_only {
        flags |= libc::O_DIRECTORY;
    }
    let fd = unsafe { libc::openat(directory.as_raw_fd(), encoded.as_ptr(), flags) };
    if fd == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(fd) })
    }
}

#[cfg(target_os = "macos")]
fn core_directory_entries(directory: &File) -> io::Result<Vec<std::ffi::OsString>> {
    use std::ffi::CStr;
    use std::os::fd::IntoRawFd;
    use std::os::unix::ffi::OsStringExt;
    let independent = core_openat(directory, std::ffi::OsStr::new("."), true)?;
    let stream = unsafe { libc::fdopendir(independent.into_raw_fd()) };
    if stream.is_null() {
        return Err(io::Error::last_os_error());
    }
    let mut names = Vec::new();
    loop {
        unsafe { *libc::__error() = 0 };
        let entry = unsafe { libc::readdir(stream) };
        if entry.is_null() {
            let error = io::Error::last_os_error();
            unsafe { libc::closedir(stream) };
            return if error.raw_os_error() == Some(0) {
                names.sort();
                Ok(names)
            } else {
                Err(error)
            };
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if name != b"." && name != b".." {
            names.push(std::ffi::OsString::from_vec(name.to_vec()));
        }
    }
}

#[cfg(target_os = "macos")]
fn core_transaction_inventory(
    root: &File,
) -> io::Result<std::collections::BTreeMap<String, (u64, u64, bool)>> {
    use std::os::unix::fs::MetadataExt;
    fn visit(
        directory: &File,
        relative: &str,
        output: &mut std::collections::BTreeMap<String, (u64, u64, bool)>,
    ) -> io::Result<()> {
        let metadata = directory.metadata()?;
        output.insert(relative.to_owned(), (metadata.dev(), metadata.ino(), true));
        for name in core_directory_entries(directory)? {
            let component = name
                .to_str()
                .ok_or_else(|| invalid_release("Core transaction name is not UTF-8"))?;
            let child_relative = if relative.is_empty() {
                component.to_owned()
            } else {
                format!("{relative}/{component}")
            };
            match core_openat(directory, &name, true) {
                Ok(child) => visit(&child, &child_relative, output)?,
                Err(error) if error.raw_os_error() == Some(libc::ENOTDIR) => {
                    let child = core_openat(directory, &name, false)?;
                    let metadata = child.metadata()?;
                    if !metadata.is_file() || metadata.nlink() != 1 {
                        return Err(invalid_release(
                            "Core transaction contains a special or linked file",
                        ));
                    }
                    output.insert(child_relative, (metadata.dev(), metadata.ino(), false));
                }
                Err(error) => return Err(error),
            }
        }
        Ok(())
    }
    let mut inventory = std::collections::BTreeMap::new();
    visit(root, "", &mut inventory)?;
    Ok(inventory)
}

#[cfg(target_os = "macos")]
fn unlinkat_core(directory: &File, name: &std::ffi::OsStr, flags: i32) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    let encoded = CString::new(name.as_bytes())
        .map_err(|_| invalid_release("Core cleanup name contains NUL"))?;
    if unsafe { libc::unlinkat(directory.as_raw_fd(), encoded.as_ptr(), flags) } == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(target_os = "macos")]
fn verify_core_transaction_name(
    parent: &File,
    name: &std::ffi::OsStr,
    expected: (u64, u64),
) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    let opened = core_openat(parent, name, true)?;
    let metadata = opened.metadata()?;
    if (metadata.dev(), metadata.ino()) != expected {
        return Err(invalid_release(
            "Core transaction name was replaced; residue retained",
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn core_transaction_remove(
    directory: &File,
    relative: &str,
    expected: &std::collections::BTreeMap<String, (u64, u64, bool)>,
) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let metadata = directory.metadata()?;
    if expected.get(relative) != Some(&(metadata.dev(), metadata.ino(), true)) {
        return Err(invalid_release(
            "Core transaction directory identity changed; residue retained",
        ));
    }
    directory.set_permissions(fs::Permissions::from_mode(0o700))?;
    for name in core_directory_entries(directory)? {
        let component = name
            .to_str()
            .ok_or_else(|| invalid_release("Core cleanup name is not UTF-8"))?;
        let child_relative = if relative.is_empty() {
            component.to_owned()
        } else {
            format!("{relative}/{component}")
        };
        let expected_identity = *expected.get(&child_relative).ok_or_else(|| {
            invalid_release("Core transaction has an unowned extra; residue retained")
        })?;
        let (child, flags) = if expected_identity.2 {
            (core_openat(directory, &name, true)?, libc::AT_REMOVEDIR)
        } else {
            (core_openat(directory, &name, false)?, 0)
        };
        let metadata = child.metadata()?;
        if (metadata.dev(), metadata.ino(), metadata.is_dir()) != expected_identity {
            return Err(invalid_release(
                "Core transaction entry was replaced; residue retained",
            ));
        }
        if expected_identity.2 {
            core_transaction_remove(&child, &child_relative, expected)?;
        }
        let current = core_openat(directory, &name, expected_identity.2)?;
        let current_metadata = current.metadata()?;
        if (
            current_metadata.dev(),
            current_metadata.ino(),
            current_metadata.is_dir(),
        ) != expected_identity
        {
            return Err(invalid_release(
                "Core transaction entry changed before unlink; residue retained",
            ));
        }
        unlinkat_core(directory, &name, flags)?;
    }
    Ok(())
}

fn resolve_tauri_shell_target_dir(project_dir: &Path) -> io::Result<PathBuf> {
    let project_dir = project_dir.canonicalize()?;
    let configured = std::env::var_os(CARGO_TARGET_DIR_ENV);
    let target = match configured {
        Some(value) if !value.is_empty() => {
            let value = PathBuf::from(value);
            reject_parent_traversal(&value, CARGO_TARGET_DIR_ENV)?;
            if value.is_absolute() {
                value
            } else {
                project_dir.join(value)
            }
        }
        _ => project_dir.join("target"),
    };
    let target = normalize_absolute_path(&target)?;
    for ancestor in target.ancestors() {
        if let Ok(metadata) = fs::symlink_metadata(ancestor) {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(invalid_release(
                    "Tauri Shell target root has an invalid component",
                ));
            }
        }
    }
    Ok(target)
}

fn resolve_core_shell_artifact(project_dir: &Path) -> io::Result<ShellArtifactAuthority> {
    let (platform, architecture) = expected_target()?;
    let target = std::env::var("TARGET")
        .map_err(|_| invalid_release("Cargo TARGET is missing for Shell resolution"))?;
    let target_root = resolve_tauri_shell_target_dir(project_dir)?;
    let (bundle_dir, filename, artifact_ref, entrypoint) = match platform.as_str() {
        "macos" => (
            "macos",
            "Tobkiri.app",
            "Tobkiri.app",
            "Tobkiri.app/Contents/MacOS/tobkiri-shell",
        ),
        "linux" => (
            "appimage",
            "Tobkiri.AppImage",
            "Tobkiri.AppImage",
            "Tobkiri.AppImage",
        ),
        "windows" => (
            "msi",
            "tobkiri-shell.exe",
            "tobkiri-shell.exe",
            "tobkiri-shell.exe",
        ),
        _ => return Err(invalid_release("unsupported Shell platform")),
    };
    let bundle_root = target_root
        .join(&target)
        .join("release/bundle")
        .join(bundle_dir);
    require_directory(&bundle_root, "intermediate Shell bundle directory")?;
    let path = bundle_root.join(filename);
    let metadata = fs::symlink_metadata(&path)?;
    if metadata.file_type().is_symlink()
        || (platform == "macos" && !metadata.is_dir())
        || (platform != "macos" && !metadata.is_file())
    {
        return Err(invalid_release(format!(
            "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: intermediate Shell artifact has an invalid type"
        )));
    }
    Ok(ShellArtifactAuthority {
        path,
        artifact_id: format!("shell.tauri.default.{platform}-{architecture}"),
        artifact_ref: artifact_ref.to_owned(),
        entrypoint: entrypoint.to_owned(),
        bundle_identity: "io.tobkiri.shell.tauri".to_owned(),
        platform,
        architecture,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ReleaseTreeEntry {
    path: String,
    directory: bool,
    size: u64,
    digest: String,
}

#[cfg(unix)]
fn reject_release_hardlink(metadata: &fs::Metadata, path: &Path) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    if metadata.is_file() && metadata.nlink() != 1 {
        return Err(invalid_release(format!(
            "presentation release file must have one link: {}",
            path.display()
        )));
    }
    Ok(())
}

#[cfg(windows)]
fn reject_release_hardlink(metadata: &fs::Metadata, path: &Path) -> io::Result<()> {
    use std::mem::MaybeUninit;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    if !metadata.is_file() {
        return Ok(());
    }
    let file = File::open(path).map_err(|error| {
        invalid_release(format!(
            "failed to inspect presentation release file links at {}: {error}",
            path.display()
        ))
    })?;
    let mut information = MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::zeroed();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) } == 0 {
        return Err(invalid_release(format!(
            "failed to inspect presentation release file links at {}: {}",
            path.display(),
            io::Error::last_os_error()
        )));
    }
    let information = unsafe { information.assume_init() };
    if information.nNumberOfLinks != 1 {
        return Err(invalid_release(format!(
            "presentation release file must have one link: {}",
            path.display()
        )));
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn reject_release_hardlink(_metadata: &fs::Metadata, _path: &Path) -> io::Result<()> {
    Ok(())
}

fn release_tree_inventory(root: &Path) -> io::Result<Vec<ReleaseTreeEntry>> {
    require_directory(root, "presentation release tree")?;
    fn visit(root: &Path, current: &Path, output: &mut Vec<ReleaseTreeEntry>) -> io::Result<()> {
        let mut entries = fs::read_dir(current)?.collect::<Result<Vec<_>, _>>()?;
        entries.sort_by_key(fs::DirEntry::file_name);
        for entry in entries {
            let path = entry.path();
            let relative = path
                .strip_prefix(root)
                .map_err(|_| invalid_release("release inventory escaped its root"))?;
            let relative_text = portable_relative_path(relative);
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_type().is_symlink() {
                return Err(invalid_release(format!(
                    "presentation release contains a symlink: {}",
                    path.display()
                )));
            }
            reject_release_hardlink(&metadata, &path)?;
            if metadata.is_dir() {
                output.push(ReleaseTreeEntry {
                    path: relative_text,
                    directory: true,
                    size: 0,
                    digest: String::new(),
                });
                visit(root, &path, output)?;
            } else if metadata.is_file() {
                output.push(ReleaseTreeEntry {
                    path: relative_text,
                    directory: false,
                    size: metadata.len(),
                    digest: byte_digest(&read_regular_file(&path, "release snapshot file")?),
                });
            } else {
                return Err(invalid_release(format!(
                    "presentation release contains an unsupported entry: {}",
                    path.display()
                )));
            }
        }
        Ok(())
    }
    let mut output = Vec::new();
    visit(root, root, &mut output)?;
    Ok(output)
}

fn verify_release_source_shape(entries: &[ReleaseTreeEntry]) -> io::Result<()> {
    let required_files = [
        "presentation_catalog.json",
        "bundled/presentation_release.v4.json",
        "bundled/shell_artifact_index.v4.json",
        "bundled/shell_profile_lock.v4.json",
        "ecosystem/defaultspack/v4/defaults.profile.v4.json",
        "ecosystem/defaultspack/v4/bundle.lock.json",
    ];
    let required_directories = [
        "bundled",
        "bundled/presentation-artifacts",
        "ecosystem",
        "ecosystem/defaultspack",
        "ecosystem/defaultspack/v4",
    ];
    for required in required_files {
        if !entries
            .iter()
            .any(|entry| !entry.directory && entry.path == required)
        {
            return Err(invalid_release(format!(
                "presentation release is missing required file: {required}"
            )));
        }
    }
    for required in required_directories {
        if !entries
            .iter()
            .any(|entry| entry.directory && entry.path == required)
        {
            return Err(invalid_release(format!(
                "presentation release is missing required directory: {required}"
            )));
        }
    }
    let mut artifact_files = 0usize;
    for entry in entries {
        let allowed = required_files.contains(&entry.path.as_str())
            || required_directories.contains(&entry.path.as_str())
            || entry.path.starts_with("bundled/presentation-artifacts/")
            || entry.path.starts_with("ecosystem/defaultspack/v4/packs");
        if !allowed {
            return Err(invalid_release(format!(
                "presentation release contains an extra entry: {}",
                entry.path
            )));
        }
        if !entry.directory && entry.path.starts_with("bundled/presentation-artifacts/") {
            artifact_files += 1;
        }
    }
    if artifact_files == 0 {
        return Err(invalid_release(
            "presentation release artifact tree is empty",
        ));
    }
    Ok(())
}

fn copy_release_tree(source: &Path, destination: &Path) -> io::Result<()> {
    fs::create_dir(destination)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(destination, fs::Permissions::from_mode(0o700))?;
    }
    let mut entries = fs::read_dir(source)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let source_path = entry.path();
        let destination_path = destination.join(entry.file_name());
        let metadata = fs::symlink_metadata(&source_path)?;
        if metadata.file_type().is_symlink() {
            return Err(invalid_release(format!(
                "presentation release snapshot source became a symlink: {}",
                source_path.display()
            )));
        }
        reject_release_hardlink(&metadata, &source_path)?;
        if metadata.is_dir() {
            copy_release_tree(&source_path, &destination_path)?;
        } else if metadata.is_file() {
            let mut input = File::open(&source_path)?;
            let opened_metadata = input.metadata()?;
            reject_release_hardlink(&opened_metadata, &source_path)?;
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&destination_path)?;
            io::copy(&mut input, &mut output)?;
            output.sync_all()?;
            let final_metadata = fs::symlink_metadata(&source_path)?;
            if final_metadata.file_type().is_symlink()
                || final_metadata.len() != opened_metadata.len()
                || input.metadata()?.len() != opened_metadata.len()
            {
                return Err(invalid_release(format!(
                    "presentation release source mutated during snapshot: {}",
                    source_path.display()
                )));
            }
            fs::set_permissions(&destination_path, opened_metadata.permissions())?;
        } else {
            return Err(invalid_release(format!(
                "presentation release snapshot source is unsupported: {}",
                source_path.display()
            )));
        }
    }
    Ok(())
}

fn seal_release_snapshot(root: &Path) -> io::Result<()> {
    for entry in release_tree_inventory(root)? {
        if entry.directory {
            continue;
        }
        let path = root.join(Path::new(&entry.path));
        let mut permissions = fs::metadata(&path)?.permissions();
        permissions.set_readonly(true);
        fs::set_permissions(path, permissions)?;
    }
    Ok(())
}

fn snapshot_presentation_release_with_hook<F>(
    source: &Path,
    destination: &Path,
    after_copy: F,
) -> io::Result<()>
where
    F: FnOnce(),
{
    let before = release_tree_inventory(source)?;
    verify_release_source_shape(&before)?;
    copy_release_tree(source, destination)?;
    after_copy();
    let after = release_tree_inventory(source)?;
    let snapshot = release_tree_inventory(destination)?;
    if before != after || before != snapshot {
        return Err(invalid_release(
            "presentation release source mutated or copied partially during snapshot",
        ));
    }
    seal_release_snapshot(destination)
}

fn snapshot_presentation_release(source: &Path, destination: &Path) -> io::Result<()> {
    snapshot_presentation_release_with_hook(source, destination, || {})
}

fn verify_release_artifact_scope(root: &Path, artifact: &Path) -> io::Result<()> {
    let artifact_root_path = root.join("bundled/presentation-artifacts");
    let selected = portable_relative_path(
        artifact
            .strip_prefix(&artifact_root_path)
            .map_err(|_| invalid_release("selected artifact escaped release artifact root"))?,
    );
    for entry in release_tree_inventory(&artifact_root_path)? {
        let ancestor = selected.starts_with(&format!("{}/", entry.path));
        let selected_or_descendant =
            entry.path == selected || entry.path.starts_with(&format!("{selected}/"));
        if !(ancestor || selected_or_descendant) {
            return Err(invalid_release(format!(
                "presentation release contains an extra artifact entry: {}",
                entry.path
            )));
        }
    }
    Ok(())
}

fn stage_presentation_release(staged_root: &Path) -> io::Result<Option<PathBuf>> {
    if core_build_stage() == CoreBuildStage::FinalApplication
        && std::env::var("DEP_TAURI_DEV").ok().as_deref() != Some("true")
    {
        return produce_and_stage_core_presentation_release(staged_root);
    }
    let Some(raw_root) = std::env::var_os(PRESENTATION_RELEASE_ROOT_ENV) else {
        if core_build_stage() == CoreBuildStage::IntermediateShell {
            println!(
                "cargo:warning=intermediate Tauri Shell build has no Launcher Presentation release; this binary is only an input to the sealed outer package"
            );
            println!("cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_B64=");
            println!("cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_ID=");
            return Ok(None);
        }
        println!("cargo:warning=development build has no sealed Presentation release; the uninstalled catalog is debug-only and cannot be packaged");
        println!("cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_B64=");
        println!("cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_ID=");
        return Ok(None);
    };
    stage_presentation_release_at(staged_root, &PathBuf::from(raw_root))
}

fn write_canonical_json(path: &Path, value: &serde_json::Value) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut bytes = serde_json::to_vec(value).map_err(io::Error::other)?;
    bytes.push(b'\n');
    fs::write(path, bytes)
}

fn selected_source_manifest_digests_from_lock(
    lock_path: &Path,
    selected: &serde_json::Map<String, serde_json::Value>,
) -> io::Result<serde_json::Map<String, serde_json::Value>> {
    let lock: serde_json::Value = serde_json::from_slice(&read_regular_file(
        lock_path,
        "generated Defaults bundle lock",
    )?)
    .map_err(|error| invalid_release(format!("Defaults lock is malformed: {error}")))?;
    let entries = lock
        .get("entries")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| invalid_release("Defaults lock entries are missing"))?;
    let mut digests = serde_json::Map::new();
    let mut paths = std::collections::BTreeSet::new();
    for entry in entries {
        let object = entry
            .as_object()
            .ok_or_else(|| invalid_release("Defaults lock entry is not an object"))?;
        if object.get("kind").and_then(serde_json::Value::as_str) != Some("pack") {
            continue;
        }
        let path = object
            .get("path")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| invalid_release("Defaults lock Pack path is missing"))?;
        let digest = object
            .get("digest")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| invalid_release("Defaults lock Pack digest is missing"))?;
        if digest.len() != 71
            || !digest.starts_with("sha256:")
            || !digest[7..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(invalid_release("Defaults lock Pack digest is invalid"));
        }
        let relative = safe_release_relative_path(path, "Defaults lock Pack path")?;
        if !path.starts_with("packs/") || !path.ends_with(".pack.v4.json") {
            return Err(invalid_release("Defaults lock Pack path is not canonical"));
        }
        if !paths.insert(path.to_owned()) {
            return Err(invalid_release(
                "Defaults lock contains a duplicate Pack path",
            ));
        }
        let pack_bytes = read_regular_file(
            &lock_path
                .parent()
                .ok_or_else(|| invalid_release("Defaults lock has no bundle root"))?
                .join(relative),
            "generated Defaults Pack",
        )?;
        if byte_digest(&pack_bytes) != digest {
            return Err(invalid_release(
                "Defaults lock Pack digest differs from exact Pack bytes",
            ));
        }
        let pack: serde_json::Value = serde_json::from_slice(&pack_bytes).map_err(|error| {
            invalid_release(format!("generated Defaults Pack is malformed: {error}"))
        })?;
        let pack_object = pack
            .as_object()
            .ok_or_else(|| invalid_release("generated Defaults Pack must be an object"))?;
        if pack_object.contains_key("pack_id")
            || pack_object
                .get("pack_api_version")
                .and_then(serde_json::Value::as_str)
                != Some("io.tobkiri.pack.v4")
        {
            return Err(invalid_release(
                "generated Defaults Pack schema is not exactly io.tobkiri.pack.v4",
            ));
        }
        let pack_id = pack
            .get("pack")
            .and_then(serde_json::Value::as_object)
            .and_then(|object| object.get("id"))
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| invalid_release("generated Defaults Pack has no pack.id"))?
            .to_owned();
        if pack_id.is_empty() || pack_id.contains('\0') {
            return Err(invalid_release(
                "generated Defaults Pack pack.id is invalid",
            ));
        }
        if selected.contains_key(&pack_id) {
            if digests
                .insert(pack_id, serde_json::Value::String(digest.to_owned()))
                .is_some()
            {
                return Err(invalid_release(
                    "Defaults lock contains a duplicate selected Pack",
                ));
            }
        }
    }
    if digests.len() != selected.len() || selected.keys().any(|key| !digests.contains_key(key)) {
        return Err(invalid_release(
            "Defaults lock is missing a selected catalog Pack",
        ));
    }
    Ok(digests)
}

fn verify_catalog_source_manifest_digests(
    catalog: &serde_json::Value,
    lock_path: &Path,
) -> io::Result<()> {
    let actual = catalog
        .get("source_manifest_digests")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| invalid_release("catalog source_manifest_digests are missing"))?;
    if actual.is_empty() {
        return Err(invalid_release("catalog selected Pack set is empty"));
    }
    let expected = selected_source_manifest_digests_from_lock(lock_path, actual)?;
    if actual != &expected {
        return Err(invalid_release(
            "catalog selected Pack set differs from the exact Defaults lock Pack entries",
        ));
    }
    Ok(())
}

fn produce_and_stage_core_presentation_release(staged_root: &Path) -> io::Result<Option<PathBuf>> {
    if core_build_stage() != CoreBuildStage::FinalApplication {
        return Err(invalid_release(
            "Core presentation producer may run only for the final application stage",
        ));
    }
    #[cfg(not(target_os = "macos"))]
    return Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Core-owned final presentation production currently requires macOS FD-anchored packaging",
    ));
    #[cfg(target_os = "macos")]
    {
        let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repository_root = project_dir
            .parent()
            .and_then(Path::parent)
            .ok_or_else(|| invalid_release("Launcher manifest has no repository root"))?
            .canonicalize()?;
        let shell = resolve_core_shell_artifact(&project_dir)?;
        let (source_identity, source_revision) = current_source_provenance()?;
        let source_tree = current_source_tree(&repository_root, &source_revision)?;
        let trusted_manifest = committed_source_manifest(&repository_root, &source_revision)?;
        let parent = staged_root
            .parent()
            .ok_or_else(|| invalid_release("staged root has no Core transaction parent"))?;
        let mut transaction = CoreTransactionGuard::create(parent)?;
        let transaction_path = transaction.path().to_owned();
        let result = (|| -> io::Result<Option<PathBuf>> {
            let release_root = transaction_path.join("release");
            let bundle_root = stage_core_defaults_bundle(&transaction, &repository_root)?;
            let artifact_root = release_root.join("bundled/presentation-artifacts");
            let installed_container = artifact_root.join(&shell.artifact_id);
            fs::create_dir_all(&installed_container)?;
            let installed_artifact = installed_container.join(
                Path::new(&shell.artifact_ref)
                    .file_name()
                    .ok_or_else(|| invalid_release("Shell artifact ref has no filename"))?,
            );
            if shell.path.is_dir() {
                copy_dir_recursive(&shell.path, &installed_artifact)?;
            } else {
                copy_file(&shell.path, &installed_artifact)?;
            }
            let snapshot_parent = transaction_path.join("source-snapshots");
            return run_formal_defaults_packaging(
                DefaultsPackagingRequest {
                    repository_root: &repository_root,
                    snapshot_parent: &snapshot_parent,
                    trusted_source_manifest: &trusted_manifest,
                    source_revision: &source_revision,
                    source_tree: &source_tree,
                    projection: DefaultsPackagingProjection {
                        source_artifact: &installed_artifact,
                        bundle_root: &bundle_root,
                        artifact_root: &release_root
                            .join("ecosystem/defaultspack/platform-artifacts"),
                        relative_path: &shell.artifact_ref,
                        entrypoint: &shell.entrypoint,
                        platform: &shell.platform,
                        architecture: &shell.architecture,
                        bundle_identity: &shell.bundle_identity,
                    },
                },
                |projection| {
                    let (artifact_digest, artifact_size) =
                        artifact_integrity::digest_and_size(&installed_artifact)?;
                    let entrypoint_path =
                        release_entrypoint(&installed_artifact, &shell.entrypoint)?;
                    let entrypoint_digest = byte_digest(&fs::read(entrypoint_path)?);
                    let relative = installed_artifact
                        .strip_prefix(&release_root)
                        .map_err(|_| invalid_release("Core artifact escaped release root"))?
                        .to_string_lossy()
                        .replace('\\', "/");
                    let mut catalog: serde_json::Value =
                        serde_json::from_slice(&read_regular_file(
                            &project_dir.join("bundled/presentation_catalog.json"),
                            "canonical presentation catalog",
                        )?)?;
                    catalog["default_profile_digest"] =
                        serde_json::Value::String(projection.default_profile_sha256.clone());
                    let selected =
                        catalog["source_manifest_digests"]
                            .as_object()
                            .ok_or_else(|| {
                                invalid_release("canonical catalog selected Pack set is missing")
                            })?;
                    let updated_source_digests = selected_source_manifest_digests_from_lock(
                        &bundle_root.join("bundle.lock.json"),
                        selected,
                    )?;
                    catalog["source_manifest_digests"] =
                        serde_json::Value::Object(updated_source_digests);
                    let variants = catalog["shell_providers"]
                        .as_array_mut()
                        .and_then(|providers| {
                            providers
                                .iter_mut()
                                .find(|provider| provider["provider_id"] == "shell.tauri.default")
                        })
                        .and_then(|provider| provider["artifact_variants"].as_array_mut())
                        .ok_or_else(|| {
                            invalid_release("canonical catalog has no Shell variants")
                        })?;
                    let variant = variants
                        .iter_mut()
                        .find(|variant| variant["artifact_id"] == shell.artifact_id)
                        .ok_or_else(|| {
                            invalid_release("canonical catalog has no target Shell variant")
                        })?;
                    variant["path"] = serde_json::Value::String(relative.clone());
                    variant["sha256"] = serde_json::Value::String(artifact_digest.clone());
                    variant["entrypoint_sha256"] =
                        serde_json::Value::String(entrypoint_digest.clone());
                    variant["size"] = serde_json::Value::from(artifact_size);
                    variant["source_identity"] = serde_json::Value::String(source_identity.clone());
                    variant["source_revision"] = serde_json::Value::String(source_revision.clone());
                    let index = serde_json::json!({
                        "schema": PRESENTATION_INDEX_SCHEMA, "artifact_id": shell.artifact_id,
                        "path": relative, "sha256": artifact_digest, "entrypoint_sha256": entrypoint_digest,
                        "size": artifact_size, "platform": shell.platform, "architecture": shell.architecture,
                        "source_identity": source_identity, "source_revision": source_revision,
                    });
                    let index_digest = canonical_value_digest(&index, "Core artifact index")?;
                    let catalog_revision = canonical_value_digest(&catalog, "Core catalog")?;
                    let lock_body = serde_json::json!({
                        "schema": PRESENTATION_LOCK_SCHEMA, "catalog_revision": catalog_revision,
                        "artifact_index_sha256": index_digest, "artifact_id": shell.artifact_id,
                        "artifact_sha256": artifact_digest, "entrypoint_sha256": entrypoint_digest,
                        "platform": shell.platform, "architecture": shell.architecture,
                        "source_identity": source_identity, "source_revision": source_revision,
                    });
                    let mut lock = lock_body.clone();
                    lock["lock_revision"] =
                        serde_json::Value::String(canonical_value_digest(&lock_body, "Core lock")?);
                    catalog["release_binding"] = serde_json::json!({
                        "schema": PRESENTATION_RELEASE_SCHEMA,
                        "artifact_index_path": "bundled/shell_artifact_index.v4.json",
                        "artifact_index_sha256": index_digest,
                        "profile_lock_path": "bundled/shell_profile_lock.v4.json",
                        "profile_lock_sha256": canonical_value_digest(&lock, "Core lock")?,
                        "catalog_revision": catalog_revision, "artifact_id": shell.artifact_id,
                        "source_identity": source_identity, "source_revision": source_revision,
                        "platform": shell.platform, "architecture": shell.architecture,
                    });
                    let catalog_path = release_root.join("presentation_catalog.json");
                    let index_path = release_root
                        .join("bundled")
                        .join(PRESENTATION_INDEX_FILENAME);
                    let lock_path = release_root
                        .join("bundled")
                        .join(PRESENTATION_LOCK_FILENAME);
                    write_canonical_json(&catalog_path, &catalog)?;
                    write_canonical_json(&index_path, &index)?;
                    write_canonical_json(&lock_path, &lock)?;
                    let mut signing_seed = [0_u8; 32];
                    rand::rngs::OsRng.fill_bytes(&mut signing_seed);
                    let signing_key = ed25519_dalek::SigningKey::from_bytes(&signing_seed);
                    let key_id = format!("core:{source_revision}:{}", shell.artifact_id);
                    let public_key = BASE64.encode(signing_key.verifying_key().to_bytes());
                    let catalog_digest = byte_digest(&fs::read(&catalog_path)?);
                    let index_file_digest = byte_digest(&fs::read(&index_path)?);
                    let lock_file_digest = byte_digest(&fs::read(&lock_path)?);
                    let message = [
                        PRESENTATION_RELEASE_SCHEMA,
                        &catalog_digest,
                        &index_file_digest,
                        &lock_file_digest,
                        &projection.default_profile_sha256,
                        &projection.defaultspack_lock_sha256,
                        &source_identity,
                        &source_revision,
                        &shell.platform,
                        &shell.architecture,
                        &shell.artifact_id,
                        &key_id,
                    ]
                    .join("\0");
                    let release = serde_json::json!({
                        "schema": PRESENTATION_RELEASE_SCHEMA, "catalog_path": "bundled/presentation_catalog.json",
                        "catalog_sha256": catalog_digest, "artifact_index_path": "bundled/shell_artifact_index.v4.json",
                        "artifact_index_sha256": index_file_digest, "profile_lock_path": "bundled/shell_profile_lock.v4.json",
                        "profile_lock_sha256": lock_file_digest,
                        "default_profile_path": "ecosystem/defaultspack/v4/defaults.profile.v4.json",
                        "default_profile_sha256": projection.default_profile_sha256,
                        "defaultspack_lock_path": "ecosystem/defaultspack/v4/bundle.lock.json",
                        "defaultspack_lock_sha256": projection.defaultspack_lock_sha256,
                        "artifact_id": shell.artifact_id, "platform": shell.platform, "architecture": shell.architecture,
                        "source_identity": source_identity, "source_revision": source_revision,
                        "key_id": key_id, "public_key": public_key,
                        "signature": BASE64.encode(signing_key.sign(message.as_bytes()).to_bytes()),
                    });
                    write_canonical_json(
                        &release_root
                            .join("bundled")
                            .join(PRESENTATION_RELEASE_FILENAME),
                        &release,
                    )?;
                    verify_presentation_release(&release_root)?;
                    stage_core_verified_release(staged_root, &release_root)
                },
            );
        })();
        match result {
            Err(error) => Err(invalid_release(format!(
                "{error}; Core transaction did not reach verified ownership, so residue was retained fail-closed"
            ))),
            Ok(output) => {
                transaction.seal_inventory().map_err(|error| {
                    invalid_release(format!(
                        "{error}; Core transaction inventory is incomplete, so verified residue was retained fail-closed"
                    ))
                })?;
                transaction.cleanup().map_err(|cleanup| {
                    invalid_release(format!("Core presentation cleanup failed: {cleanup}"))
                })?;
                Ok(output)
            }
        }
    }
}

fn stage_presentation_release_at(
    staged_root: &Path,
    release_root: &Path,
) -> io::Result<Option<PathBuf>> {
    require_directory(release_root, "release presentation root")?;
    let snapshot_parent = staged_root
        .parent()
        .ok_or_else(|| invalid_release("staged root has no private snapshot parent"))?;
    let snapshot_root = snapshot_parent.join(format!(
        ".tobkiri-presentation-release-snapshot-{}",
        std::process::id()
    ));
    if fs::symlink_metadata(&snapshot_root).is_ok() {
        return Err(invalid_release(format!(
            "private presentation snapshot already exists: {}",
            snapshot_root.display()
        )));
    }
    let result = (|| {
        snapshot_presentation_release(release_root, &snapshot_root)?;
        stage_presentation_release_from_snapshot(staged_root, &snapshot_root)
    })();
    if snapshot_root.exists() {
        fs::remove_dir_all(&snapshot_root)?;
    }
    result
}

fn stage_presentation_release_from_snapshot(
    staged_root: &Path,
    release_root: &Path,
) -> io::Result<Option<PathBuf>> {
    let catalog = release_root.join(PRESENTATION_CATALOG_FILENAME);
    require_regular_file(&catalog, "release presentation catalog")?;

    let release_bundled = release_root.join("bundled");
    require_directory(&release_bundled, "release presentation bundle directory")?;
    let artifacts = release_bundled.join("presentation-artifacts");
    require_directory(&artifacts, "release presentation artifacts")?;
    for filename in [
        PRESENTATION_RELEASE_FILENAME,
        PRESENTATION_INDEX_FILENAME,
        PRESENTATION_LOCK_FILENAME,
    ] {
        require_regular_file(
            &release_bundled.join(filename),
            "release presentation binding file",
        )?;
    }

    let verified = verify_presentation_release(release_root)?;
    println!(
        "cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_B64={}",
        verified.public_key
    );
    println!(
        "cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_ID={}",
        verified.key_id
    );

    let staged_bundled = staged_root.join("bundled");
    copy_file(
        &catalog,
        &staged_bundled.join(PRESENTATION_CATALOG_FILENAME),
    )?;
    copy_dir_recursive(&artifacts, &staged_bundled.join("presentation-artifacts"))?;
    for filename in [
        PRESENTATION_RELEASE_FILENAME,
        PRESENTATION_INDEX_FILENAME,
        PRESENTATION_LOCK_FILENAME,
    ] {
        copy_file(
            &release_bundled.join(filename),
            &staged_bundled.join(filename),
        )?;
    }
    let bundle_root = staged_root.join("ecosystem/defaultspack/v4");
    #[cfg(not(test))]
    if !bundle_root.is_dir() {
        return Err(invalid_release(
            "complete staged verification requires the packaged Defaults v4 bundle",
        ));
    }
    if bundle_root.is_dir() {
        let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .ok_or_else(|| invalid_release("Launcher manifest has no repository root"))?
            .canonicalize()
            .map_err(|error| {
                invalid_release(format!("failed to resolve repository root: {error}"))
            })?;
        let source_revision = current_source_revision(&repository_root)?;
        let source_tree = current_source_tree(&repository_root, &source_revision)?;
        let trusted_source_manifest =
            committed_source_manifest(&repository_root, &source_revision)?;
        let snapshot_parent = std::env::var_os("OUT_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| staged_root.join(".verified-source-snapshots"));
        let artifact_root = staged_root.join("ecosystem/defaultspack/platform-artifacts");
        let staged_catalog = staged_bundled.join(PRESENTATION_CATALOG_FILENAME);
        return run_formal_defaults_packaging(
            DefaultsPackagingRequest {
                repository_root: &repository_root,
                snapshot_parent: &snapshot_parent,
                trusted_source_manifest: &trusted_source_manifest,
                source_revision: &source_revision,
                source_tree: &source_tree,
                projection: DefaultsPackagingProjection {
                    source_artifact: &verified.artifact_path,
                    bundle_root: &bundle_root,
                    artifact_root: &artifact_root,
                    relative_path: &verified.artifact_ref,
                    entrypoint: &verified.entrypoint,
                    platform: &verified.platform,
                    architecture: &verified.architecture,
                    bundle_identity: &verified.bundle_identity,
                },
            },
            |output| {
                if output.default_profile_sha256 != verified.default_profile_sha256
                    || output.defaultspack_lock_sha256 != verified.defaultspack_lock_sha256
                {
                    return Err(invalid_release(format!(
                    "{FORMAL_DEFAULTS_PACKAGING_COMMAND}: projection differs from signed release identities"
                )));
                }
                let staged_verified = verify_presentation_release_at(staged_root, &staged_catalog)?;
                if staged_verified.artifact_ref != verified.artifact_ref
                    || staged_verified.entrypoint != verified.entrypoint
                {
                    return Err(invalid_release(
                        "complete staged presentation release differs from its verified snapshot",
                    ));
                }
                Ok(Some(staged_catalog))
            },
        );
    }
    let staged_catalog = staged_bundled.join(PRESENTATION_CATALOG_FILENAME);
    Ok(Some(staged_catalog))
}

fn stage_core_verified_release(
    staged_root: &Path,
    release_root: &Path,
) -> io::Result<Option<PathBuf>> {
    let verified = verify_presentation_release(release_root)?;
    let staged_bundled = staged_root.join("bundled");
    let release_bundled = release_root.join("bundled");
    copy_file(
        &release_root.join(PRESENTATION_CATALOG_FILENAME),
        &staged_bundled.join(PRESENTATION_CATALOG_FILENAME),
    )?;
    copy_dir_recursive(
        &release_bundled.join("presentation-artifacts"),
        &staged_bundled.join("presentation-artifacts"),
    )?;
    for filename in [
        PRESENTATION_RELEASE_FILENAME,
        PRESENTATION_INDEX_FILENAME,
        PRESENTATION_LOCK_FILENAME,
    ] {
        copy_file(
            &release_bundled.join(filename),
            &staged_bundled.join(filename),
        )?;
    }
    let staged_defaults_bundle = staged_root.join("ecosystem/defaultspack/v4");
    copy_dir_recursive(
        &release_root.join("ecosystem/defaultspack/v4"),
        &staged_defaults_bundle,
    )?;
    remove_source_only_profile_artifacts(&staged_defaults_bundle)?;
    let platform_artifacts = release_root.join("ecosystem/defaultspack/platform-artifacts");
    if platform_artifacts.is_dir() {
        copy_dir_recursive(
            &platform_artifacts,
            &staged_root.join("ecosystem/defaultspack/platform-artifacts"),
        )?;
    }
    let staged_catalog = staged_bundled.join(PRESENTATION_CATALOG_FILENAME);
    let staged_verified = verify_presentation_release_at(staged_root, &staged_catalog)?;
    if staged_verified.artifact_ref != verified.artifact_ref
        || staged_verified.entrypoint != verified.entrypoint
    {
        return Err(invalid_release(
            "Core-staged presentation release differs from its verified authority",
        ));
    }
    println!(
        "cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_B64={}",
        verified.public_key
    );
    println!(
        "cargo:rustc-env=TOBKIRI_PRESENTATION_TRUST_KEY_ID={}",
        verified.key_id
    );
    Ok(Some(staged_catalog))
}

fn remove_source_only_profile_artifacts(bundle_root: &Path) -> io::Result<()> {
    require_directory(bundle_root, "staged packaged Defaults v4 bundle")?;
    for filename in SOURCE_ONLY_PROFILE_ARTIFACTS {
        let candidate = bundle_root.join(filename);
        let metadata = match fs::symlink_metadata(&candidate) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if metadata.file_type().is_symlink() {
            return Err(invalid_release(format!(
                "staged source-only Profile artifact may not be a symlink: {}",
                candidate.display()
            )));
        }
        if !metadata.is_file() {
            return Err(invalid_release(format!(
                "staged source-only Profile artifact must be absent or regular: {}",
                candidate.display()
            )));
        }
        reject_release_hardlink(&metadata, &candidate)?;
        fs::remove_file(&candidate)?;
        match fs::symlink_metadata(&candidate) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Ok(_) => {
                return Err(invalid_release(format!(
                    "staged source-only Profile artifact remained after removal: {}",
                    candidate.display()
                )));
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn current_source_revision(repository_root: &Path) -> io::Result<String> {
    let git = packaging_toolchain::verified_tool("git")?;
    let revision = git
        .command()?
        .args(["rev-parse", "--verify", "HEAD^{commit}"])
        .current_dir(repository_root)?
        .output()
        .map_err(|error| invalid_release(format!("failed to read source revision: {error}")))?;
    if !revision.status.success() {
        return Err(invalid_release("source checkout has no verifiable commit"));
    }
    let value = String::from_utf8(revision.stdout)
        .map_err(|error| invalid_release(format!("source revision is not UTF-8: {error}")))?
        .trim()
        .to_owned();
    if value.len() != 40
        || !value
            .chars()
            .all(|character| character.is_ascii_digit() || ('a'..='f').contains(&character))
    {
        return Err(invalid_release(
            "production source revision must be a full lowercase commit SHA",
        ));
    }
    let clean_index = git
        .command()?
        .args([
            "diff-index",
            "--cached",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            &value,
            "--",
        ])
        .current_dir(repository_root)?
        .status()
        .map_err(|error| invalid_release(format!("failed to inspect source index: {error}")))?;
    if !clean_index.success() {
        return Err(invalid_release(
            "production source index does not match its revision",
        ));
    }
    verify_tracked_worktree_bytes(&git, repository_root, &value)?;
    let untracked = git
        .command()?
        .args(["ls-files", "--others", "-z", "--"])
        .current_dir(repository_root)?
        .output()
        .map_err(|error| invalid_release(format!("failed to inspect untracked source: {error}")))?;
    if !untracked.status.success() {
        return Err(invalid_release(
            "source checkout untracked inventory could not be verified",
        ));
    }
    for encoded in untracked
        .stdout
        .split(|byte| *byte == 0)
        .filter(|path| !path.is_empty())
    {
        let relative = std::str::from_utf8(encoded)
            .map_err(|_| invalid_release("untracked source path is not UTF-8"))?;
        verify_allowed_generated_untracked(repository_root, relative)?;
    }
    Ok(value)
}

fn verify_allowed_generated_untracked(repository_root: &Path, relative: &str) -> io::Result<()> {
    const GENERATED_ROOTS: &[(&str, bool)] = &[
        ("pack-shell/target", false),
        ("tobkiri_launcher/src-tauri/target", false),
        ("tobkiri_launcher/src-tauri/gen", false),
        ("tobkiri_launcher/frontend/node_modules", true),
        (
            "tobkiri_runtime/ecosystem/defaultspack/webapp/node_modules",
            true,
        ),
        ("tobkiri_runtime/ecosystem/defaultspack/webapp/dist", false),
    ];
    let relative_path = safe_release_relative_path(relative, "untracked source path")?;
    let (root, allow_symlink) = GENERATED_ROOTS
        .iter()
        .find(|(root, _)| relative == *root || relative.starts_with(&format!("{root}/")))
        .copied()
        .ok_or_else(|| {
            invalid_release(format!(
                "production source revision cannot describe untracked path: {relative}"
            ))
        })?;
    let root_relative = Path::new(root);
    let descendant = relative_path.strip_prefix(root_relative).map_err(|_| {
        invalid_release(format!(
            "generated untracked path escaped its root: {relative}"
        ))
    })?;
    let mut current = repository_root.join(root_relative);
    let mut metadata = fs::symlink_metadata(&current)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(invalid_release(format!(
            "generated untracked root has unsafe type: {root}"
        )));
    }
    if allow_symlink {
        return verify_node_modules_generated_path(&current, descendant, relative);
    }
    let components = descendant.components().collect::<Vec<_>>();
    for (index, component) in components.iter().enumerate() {
        current.push(component.as_os_str());
        metadata = fs::symlink_metadata(&current)?;
        let is_leaf = index + 1 == components.len();
        if metadata.file_type().is_symlink() {
            return Err(invalid_release(format!(
                "generated untracked path contains a symlink: {relative}"
            )));
        } else if is_leaf {
            if !(metadata.is_file() || metadata.is_dir()) {
                return Err(invalid_release(format!(
                    "generated untracked path has unsafe type: {relative}"
                )));
            }
            #[cfg(unix)]
            if metadata.is_file() {
                use std::os::unix::fs::MetadataExt;
                if metadata.nlink() != 1 {
                    return Err(invalid_release(format!(
                        "generated untracked regular file is hardlinked: {relative}"
                    )));
                }
            }
        } else if !metadata.is_dir() {
            return Err(invalid_release(format!(
                "generated untracked ancestor is not a directory: {relative}"
            )));
        }
    }
    Ok(())
}

#[cfg(unix)]
fn verify_node_modules_generated_path(
    root: &Path,
    descendant: &Path,
    relative: &str,
) -> io::Result<()> {
    const MAX_SYMLINK_HOPS: usize = 40;
    use std::ffi::{CString, OsStr, OsString};
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    use std::os::unix::fs::MetadataExt;

    fn open_root(root: &Path, relative: &str) -> io::Result<File> {
        let before = fs::symlink_metadata(root)?;
        if before.file_type().is_symlink() || !before.is_dir() {
            return Err(invalid_release(format!(
                "generated node_modules root has unsafe type: {relative}"
            )));
        }
        let encoded = CString::new(root.as_os_str().as_bytes())
            .map_err(|_| invalid_release("generated node_modules root contains NUL"))?;
        let fd = unsafe {
            libc::open(
                encoded.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd == -1 {
            return Err(io::Error::last_os_error());
        }
        let handle = unsafe { File::from_raw_fd(fd) };
        let opened = handle.metadata()?;
        let after = fs::symlink_metadata(root)?;
        if !same_source_object(&before, &opened) || !same_source_object(&opened, &after) {
            return Err(invalid_release(format!(
                "generated node_modules root identity changed during verification: {relative}"
            )));
        }
        Ok(handle)
    }

    fn lstat_at(directory: &File, name: &OsStr) -> io::Result<libc::stat> {
        let encoded = CString::new(name.as_bytes())
            .map_err(|_| invalid_release("generated node_modules component contains NUL"))?;
        let mut status = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                directory.as_raw_fd(),
                encoded.as_ptr(),
                status.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } == -1
        {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { status.assume_init() })
    }

    fn readlink_at(directory: &File, name: &OsStr) -> io::Result<PathBuf> {
        const MAX_LINK_BYTES: usize = 65_536;
        let encoded = CString::new(name.as_bytes())
            .map_err(|_| invalid_release("generated node_modules symlink contains NUL"))?;
        let mut capacity = 256_usize;
        loop {
            let mut bytes = vec![0_u8; capacity];
            let length = unsafe {
                libc::readlinkat(
                    directory.as_raw_fd(),
                    encoded.as_ptr(),
                    bytes.as_mut_ptr().cast(),
                    bytes.len(),
                )
            };
            if length == -1 {
                return Err(io::Error::last_os_error());
            }
            let length = length as usize;
            if length < bytes.len() {
                bytes.truncate(length);
                return Ok(PathBuf::from(OsString::from_vec(bytes)));
            }
            if capacity == MAX_LINK_BYTES {
                return Err(invalid_release(
                    "generated node_modules symlink target is too long",
                ));
            }
            capacity = (capacity * 2).min(MAX_LINK_BYTES);
        }
    }

    fn open_at(directory: &File, name: &OsStr, directory_only: bool) -> io::Result<File> {
        let encoded = CString::new(name.as_bytes())
            .map_err(|_| invalid_release("generated node_modules component contains NUL"))?;
        let mut flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
        if directory_only {
            flags |= libc::O_DIRECTORY;
        }
        let fd = unsafe { libc::openat(directory.as_raw_fd(), encoded.as_ptr(), flags) };
        if fd == -1 {
            Err(io::Error::last_os_error())
        } else {
            Ok(unsafe { File::from_raw_fd(fd) })
        }
    }

    fn same_stat_object(status: &libc::stat, metadata: &fs::Metadata) -> bool {
        status.st_dev as u64 == metadata.dev()
            && status.st_ino as u64 == metadata.ino()
            && (status.st_mode as u32 & libc::S_IFMT as u32)
                == (metadata.mode() & libc::S_IFMT as u32)
    }

    let root_handle = open_root(root, relative)?;
    let mut directory = root_handle.try_clone()?;
    let mut pending = descendant
        .components()
        .map(|component| component.as_os_str().to_owned())
        .collect::<std::collections::VecDeque<_>>();
    let mut resolved = Vec::<OsString>::new();
    let mut visited_links = std::collections::HashSet::<PathBuf>::new();
    let mut hops = 0_usize;

    if pending.is_empty() {
        return verify_generated_open_target(&root_handle, relative);
    }
    while let Some(component) = pending.pop_front() {
        let status = lstat_at(&directory, &component)?;
        let logical = resolved
            .iter()
            .fold(PathBuf::new(), |path, component| path.join(component))
            .join(&component);
        if status.st_mode as u32 & libc::S_IFMT as u32 == libc::S_IFLNK as u32 {
            hops += 1;
            if hops > MAX_SYMLINK_HOPS || !visited_links.insert(logical) {
                return Err(invalid_release(format!(
                    "generated node_modules symlink chain is cyclic or too deep: {relative}"
                )));
            }
            let target = readlink_at(&directory, &component)?;
            if target.is_absolute() {
                return Err(invalid_release(format!(
                    "generated node_modules symlink target is absolute: {relative}"
                )));
            }
            let mut replacement = resolved.clone();
            for target_component in target.components() {
                match target_component {
                    Component::CurDir => {}
                    Component::Normal(value) => replacement.push(value.to_owned()),
                    Component::ParentDir => {
                        if replacement.pop().is_none() {
                            return Err(invalid_release(format!(
                                "generated node_modules symlink escapes its root: {relative}"
                            )));
                        }
                    }
                    Component::RootDir | Component::Prefix(_) => {
                        return Err(invalid_release(format!(
                            "generated node_modules symlink target is absolute: {relative}"
                        )))
                    }
                }
            }
            replacement.extend(pending.drain(..));
            pending = replacement.into();
            resolved.clear();
            directory = root_handle.try_clone()?;
            continue;
        }

        let is_leaf = pending.is_empty();
        let opened = open_at(&directory, &component, !is_leaf)?;
        let metadata = opened.metadata()?;
        let after = lstat_at(&directory, &component)?;
        if !same_stat_object(&status, &metadata) || !same_stat_object(&after, &metadata) {
            return Err(invalid_release(format!(
                "generated node_modules component identity changed: {relative}"
            )));
        }
        if is_leaf {
            return verify_generated_open_target(&opened, relative);
        }
        if !metadata.is_dir() {
            return Err(invalid_release(format!(
                "generated node_modules ancestor is not a directory: {relative}"
            )));
        }
        directory = opened;
        resolved.push(component);
    }
    Err(invalid_release(format!(
        "generated node_modules path has no target: {relative}"
    )))
}

#[cfg(unix)]
fn verify_generated_open_target(handle: &File, relative: &str) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    let metadata = handle.metadata()?;
    if !(metadata.is_file() || metadata.is_dir()) {
        return Err(invalid_release(format!(
            "generated untracked target has unsafe type: {relative}"
        )));
    }
    if metadata.is_file() && metadata.nlink() != 1 {
        return Err(invalid_release(format!(
            "generated untracked target is hardlinked: {relative}"
        )));
    }
    Ok(())
}

#[cfg(not(unix))]
fn verify_node_modules_generated_path(
    _root: &Path,
    _descendant: &Path,
    _relative: &str,
) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "node_modules generated output requires Unix FD-relative verification",
    ))
}

fn verify_tracked_worktree_bytes(
    git: &packaging_toolchain::VerifiedTool,
    repository_root: &Path,
    revision: &str,
) -> io::Result<()> {
    let tree = git
        .command()?
        .args(["ls-tree", "-r", "-z", "--full-tree", revision])
        .current_dir(repository_root)?
        .output()
        .map_err(|error| invalid_release(format!("failed to read source tree: {error}")))?;
    if !tree.status.success() {
        return Err(invalid_release("source tree inventory could not be read"));
    }
    for record in tree
        .stdout
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
    {
        let separator = record
            .iter()
            .position(|byte| *byte == b'\t')
            .ok_or_else(|| invalid_release("source tree entry is malformed"))?;
        let (header, encoded_path) = record.split_at(separator);
        let encoded_path = &encoded_path[1..];
        let header = std::str::from_utf8(header)
            .map_err(|_| invalid_release("source tree header is not UTF-8"))?;
        let mut fields = header.split(' ');
        let mode = fields
            .next()
            .ok_or_else(|| invalid_release("source tree mode is missing"))?;
        let kind = fields
            .next()
            .ok_or_else(|| invalid_release("source tree kind is missing"))?;
        let expected = fields
            .next()
            .ok_or_else(|| invalid_release("source tree object ID is missing"))?;
        if fields.next().is_some()
            || kind != "blob"
            || !matches!(mode, "100644" | "100755" | "120000")
        {
            return Err(invalid_release("source tree entry is unsupported"));
        }
        let relative = std::str::from_utf8(encoded_path)
            .map_err(|_| invalid_release("source tree path is not UTF-8"))?;
        let relative = safe_release_relative_path(relative, "source tree path")?;
        let path = repository_root.join(&relative);
        let before = fs::symlink_metadata(&path)?;
        let payload = if mode == "120000" {
            if !before.file_type().is_symlink() {
                return Err(invalid_release(format!(
                    "tracked symlink type changed: {}",
                    relative.display()
                )));
            }
            fs::read_link(&path)?
                .as_os_str()
                .as_encoded_bytes()
                .to_vec()
        } else {
            if !before.is_file() || before.file_type().is_symlink() {
                return Err(invalid_release(format!(
                    "tracked file type changed: {}",
                    relative.display()
                )));
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if (before.permissions().mode() & 0o111 != 0) != (mode == "100755") {
                    return Err(invalid_release(format!(
                        "tracked executable mode changed: {}",
                        relative.display()
                    )));
                }
            }
            let mut file = File::open(&path)?;
            let opened = file.metadata()?;
            if !same_source_object(&before, &opened) {
                return Err(invalid_release(format!(
                    "tracked file changed before read: {}",
                    relative.display()
                )));
            }
            let mut payload = Vec::new();
            file.read_to_end(&mut payload)?;
            if !same_source_object(&opened, &file.metadata()?) {
                return Err(invalid_release(format!(
                    "tracked file changed while read: {}",
                    relative.display()
                )));
            }
            payload
        };
        let after = fs::symlink_metadata(&path)?;
        if !same_source_object(&before, &after) {
            return Err(invalid_release(format!(
                "tracked path changed while verified: {}",
                relative.display()
            )));
        }
        let header = format!("blob {}\0", payload.len());
        let actual = match expected.len() {
            40 => {
                let mut digest = Sha1::new();
                digest.update(header.as_bytes());
                digest.update(&payload);
                format!("{:x}", digest.finalize())
            }
            64 => {
                let mut digest = Sha256::new();
                digest.update(header.as_bytes());
                digest.update(&payload);
                format!("{:x}", digest.finalize())
            }
            _ => {
                return Err(invalid_release(
                    "source tree object ID length is unsupported",
                ))
            }
        };
        if actual != expected {
            return Err(invalid_release(format!(
                "tracked file bytes changed: {}",
                relative.display()
            )));
        }
    }
    Ok(())
}

fn same_source_object(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        left.dev() == right.dev()
            && left.ino() == right.ino()
            && left.file_type() == right.file_type()
            && left.len() == right.len()
            && left.mtime() == right.mtime()
            && left.mtime_nsec() == right.mtime_nsec()
            && left.ctime() == right.ctime()
            && left.ctime_nsec() == right.ctime_nsec()
    }
    #[cfg(not(unix))]
    {
        left.file_type() == right.file_type()
            && left.len() == right.len()
            && left.modified().ok() == right.modified().ok()
    }
}

fn current_source_tree(repository_root: &Path, revision: &str) -> io::Result<String> {
    let git = packaging_toolchain::verified_tool("git")?;
    let object = format!("{revision}^{{tree}}");
    let output = git
        .command()?
        .args(["rev-parse", "--verify", &object])
        .current_dir(repository_root)?
        .output()
        .map_err(|error| invalid_release(format!("failed to read source tree: {error}")))?;
    if !output.status.success() {
        return Err(invalid_release("source checkout has no verifiable tree"));
    }
    let value = String::from_utf8(output.stdout)
        .map_err(|error| invalid_release(format!("source tree is not UTF-8: {error}")))?
        .trim()
        .to_owned();
    if !matches!(value.len(), 40 | 64)
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(invalid_release(
            "source tree is not a canonical Git object ID",
        ));
    }
    Ok(value)
}

fn committed_source_manifest(repository_root: &Path, revision: &str) -> io::Result<Vec<u8>> {
    let git = packaging_toolchain::verified_tool("git")?;
    let object =
        format!("{revision}:tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json");
    let output = git
        .command()?
        .args(["show", &object])
        .current_dir(repository_root)?
        .output()
        .map_err(|error| {
            invalid_release(format!("failed to read committed source manifest: {error}"))
        })?;
    if !output.status.success() || output.stdout.len() > 4 * 1024 * 1024 {
        return Err(invalid_release(
            "committed packaged source manifest is unavailable or oversized",
        ));
    }
    Ok(output.stdout)
}

fn verify_staged_catalog(source_catalog: &Path, staged_catalog: &Path) -> io::Result<()> {
    let expected = read_regular_file(source_catalog, "source presentation catalog")?;
    let actual = read_regular_file(staged_catalog, "staged presentation catalog")?;
    if expected != actual {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "manifest-derived presentation catalog differs between {} and {}",
                source_catalog.display(),
                staged_catalog.display()
            ),
        ));
    }
    Ok(())
}

fn require_regular_file(path: &Path, label: &str) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path).map_err(|error| {
        if error.kind() == io::ErrorKind::NotFound {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!("{label} is missing at {}", path.display()),
            )
        } else {
            error
        }
    })?;
    if metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{label} may not be a symlink: {}", path.display()),
        ));
    }
    if !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{label} must be a regular file: {}", path.display()),
        ));
    }
    Ok(())
}

fn require_directory(path: &Path, label: &str) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path).map_err(|error| {
        if error.kind() == io::ErrorKind::NotFound {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!("{label} is missing at {}", path.display()),
            )
        } else {
            error
        }
    })?;
    if metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{label} may not be a symlink: {}", path.display()),
        ));
    }
    if !metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{label} must be a directory: {}", path.display()),
        ));
    }
    Ok(())
}

fn read_regular_file(path: &Path, label: &str) -> io::Result<Vec<u8>> {
    require_regular_file(path, label)?;
    fs::read(path)
}

fn stage_error(step: &str, error: io::Error) -> io::Error {
    io::Error::new(error.kind(), format!("{step}: {error}"))
}

fn stage_setup_brand_icon(repo_root: &Path, staged_root: &Path) -> io::Result<()> {
    let icon_source = repo_root
        .join("tobkiri_launcher")
        .join("assets")
        .join("app-icon")
        .join("tobkiri-launcher-icon.png");
    let icon_target = staged_root
        .join("core_runtime")
        .join("core_pack")
        .join("core_setup")
        .join("web")
        .join("assets")
        .join("tobkiri-launcher-icon.png");
    copy_file(&icon_source, &icon_target).map(|_| ())
}

fn stage_pack_shell(repo_root: &Path, staged_root: &Path) -> io::Result<()> {
    let Some(pack_shell) = ensure_pack_shell_binary(repo_root)? else {
        return Ok(());
    };
    let target = required_cargo_target()?;
    let (payload, permissions) = read_verified_pack_shell(&pack_shell, &target)?;
    verify_prebuilt_pack_shell_digest(&pack_shell, &payload)?;
    let bundled_dir = staged_root.join("bundled");
    if bundled_dir.exists() {
        require_directory(&bundled_dir, "pack-shell staging directory")?;
    }
    fs::create_dir_all(&bundled_dir)?;
    let destination = bundled_dir.join(pack_shell_binary_name(&target));
    if destination.exists() || fs::symlink_metadata(&destination).is_ok() {
        require_regular_file(&destination, "pack-shell staging destination")?;
    }
    let temporary = bundled_dir.join(format!(
        ".{}.{}.tmp",
        pack_shell_binary_name(&target),
        std::process::id()
    ));
    let mut temporary_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    let stage_result = (|| {
        temporary_file.write_all(&payload)?;
        temporary_file.sync_all()?;
        fs::set_permissions(&temporary, permissions)?;
        drop(temporary_file);
        if destination.exists() {
            fs::remove_file(&destination)?;
        }
        fs::rename(&temporary, &destination)?;
        let staged = fs::read(&destination)?;
        if Sha256::digest(&staged) != Sha256::digest(&payload) {
            let _ = fs::remove_file(&destination);
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "staged pack-shell SHA256 mismatch: {}",
                    destination.display()
                ),
            ));
        }
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    stage_result?;
    Ok(())
}

fn ensure_pack_shell_binary(repo_root: &Path) -> io::Result<Option<PathBuf>> {
    if let Some(pack_shell) = find_pack_shell_binary(repo_root)? {
        return Ok(Some(pack_shell));
    }

    let manifest = repo_root.join("pack-shell").join("Cargo.toml");
    if !manifest.is_file() {
        return Ok(None);
    }

    Err(io::Error::new(
        io::ErrorKind::NotFound,
        format!(
            "prebuilt verified pack-shell is required; production staging may not build source from {}",
            manifest.display()
        ),
    ))
}

fn verify_prebuilt_pack_shell_digest(path: &Path, payload: &[u8]) -> io::Result<()> {
    let mut digest_name = path.as_os_str().to_os_string();
    digest_name.push(".sha256");
    let digest_path = PathBuf::from(digest_name);
    let expected = String::from_utf8(read_regular_file(
        &digest_path,
        "prebuilt pack-shell digest",
    )?)
    .map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "prebuilt pack-shell digest must be UTF-8",
        )
    })?;
    let actual = format!("{:x}\n", Sha256::digest(payload));
    if expected != actual {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "prebuilt pack-shell digest mismatch or non-canonical encoding",
        ));
    }
    Ok(())
}

fn find_pack_shell_binary(repo_root: &Path) -> io::Result<Option<PathBuf>> {
    let target = required_cargo_target()?;
    let profile = required_cargo_profile()?;

    let target_dir = resolve_cargo_target_dir(repo_root)?;
    let candidate = target_dir
        .join(&target)
        .join(&profile)
        .join(pack_shell_binary_name(&target));

    let metadata = match fs::symlink_metadata(&candidate) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "pack-shell binary may not be a symlink: {}",
                candidate.display()
            ),
        ));
    }
    if !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "pack-shell binary must be a regular file: {}",
                candidate.display()
            ),
        ));
    }

    #[cfg(unix)]
    let is_executable = {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    };
    #[cfg(unix)]
    if !is_executable {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "pack-shell binary must be executable: {}",
                candidate.display()
            ),
        ));
    }

    let canonical = candidate.canonicalize()?;
    if canonical != candidate {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "pack-shell binary path is not canonical or contains a symlink: {}",
                candidate.display()
            ),
        ));
    }
    Ok(Some(canonical))
}

fn resolve_cargo_target_dir(repo_root: &Path) -> io::Result<PathBuf> {
    let repository_root = repo_root.canonicalize()?;
    let target_dir = match std::env::var_os(CARGO_TARGET_DIR_ENV) {
        Some(configured) if !configured.is_empty() => {
            let configured = PathBuf::from(configured);
            reject_parent_traversal(&configured, CARGO_TARGET_DIR_ENV)?;
            if configured.is_absolute() {
                configured
            } else {
                repository_root.join(configured)
            }
        }
        _ => repository_root.join("pack-shell").join("target"),
    };

    let target_dir = normalize_absolute_path(&target_dir)?;
    let mut ancestors = target_dir.ancestors().collect::<Vec<_>>();
    ancestors.reverse();
    for ancestor in ancestors {
        match fs::symlink_metadata(ancestor) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "Cargo target directory contains a symlink component: {}",
                        target_dir.display()
                    ),
                ));
            }
            Ok(metadata) if !metadata.is_dir() => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "Cargo target directory has a non-directory component: {}",
                        ancestor.display()
                    ),
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(target_dir)
}

fn reject_parent_traversal(path: &Path, label: &str) -> io::Result<()> {
    if path
        .components()
        .any(|component| component == Component::ParentDir)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{label} may not contain parent traversal: {path:?}"),
        ));
    }
    Ok(())
}

fn normalize_absolute_path(path: &Path) -> io::Result<PathBuf> {
    if !path.is_absolute() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("path must be absolute after resolution: {}", path.display()),
        ));
    }
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!(
                        "resolved path contains parent traversal: {}",
                        path.display()
                    ),
                ));
            }
            _ => normalized.push(component.as_os_str()),
        }
    }
    Ok(normalized)
}

fn required_cargo_target() -> io::Result<String> {
    let target = std::env::var("TARGET").map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "Cargo TARGET is missing or non-Unicode",
        )
    })?;
    validate_path_component(&target, "Rust target")?;
    Ok(target)
}

fn required_cargo_profile() -> io::Result<String> {
    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "debug".to_string());
    validate_path_component(&profile, "Cargo profile")?;
    Ok(profile)
}

fn validate_path_component(value: &str, label: &str) -> io::Result<()> {
    if value.is_empty()
        || value == "."
        || value == ".."
        || value.contains('/')
        || value.contains('\\')
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{label} must be a single path component: {value:?}"),
        ));
    }
    Ok(())
}

fn expected_pack_shell_architecture(target: &str) -> &str {
    match target.split('-').next().unwrap_or_default() {
        "amd64" => "x86_64",
        "arm64" => "aarch64",
        "i586" | "i686" => "x86",
        architecture => architecture,
    }
}

fn expected_sealed_python_target(target: &str) -> io::Result<(&'static str, &'static str)> {
    match target {
        "aarch64-apple-darwin" => Ok(("macos", "arm64")),
        "x86_64-apple-darwin" => Ok(("macos", "x86_64")),
        "x86_64-pc-windows-msvc" => Ok(("windows", "x86_64")),
        "x86_64-unknown-linux-gnu" => Ok(("linux", "x86_64")),
        _ => Err(io::Error::new(
            io::ErrorKind::Unsupported,
            format!("sealed Python target platform is unsupported: {target}"),
        )),
    }
}

fn pack_shell_binary_architecture(payload: &[u8], target: &str) -> io::Result<String> {
    let invalid = |message: &str| io::Error::new(io::ErrorKind::InvalidData, message);
    if target.contains("windows") || target.ends_with("-msvc") {
        if payload.len() < 64 || &payload[..2] != b"MZ" {
            return Err(invalid("pack-shell is not a PE executable"));
        }
        let pe_offset = u32::from_le_bytes(payload[60..64].try_into().unwrap()) as usize;
        if payload.len() < pe_offset + 6 || &payload[pe_offset..pe_offset + 4] != b"PE\0\0" {
            return Err(invalid("pack-shell has an invalid PE header"));
        }
        let machine = u16::from_le_bytes(payload[pe_offset + 4..pe_offset + 6].try_into().unwrap());
        return Ok(match machine {
            0x014c => "x86".to_string(),
            0x8664 => "x86_64".to_string(),
            0xaa64 => "aarch64".to_string(),
            _ => format!("pe-machine-{machine:#x}"),
        });
    }

    if target.contains("apple-darwin") {
        if payload.len() < 8 {
            return Err(invalid("pack-shell has a truncated Mach-O header"));
        }
        let cpu_type = match &payload[..4] {
            b"\xcf\xfa\xed\xfe" | b"\xce\xfa\xed\xfe" => {
                u32::from_le_bytes(payload[4..8].try_into().unwrap())
            }
            b"\xfe\xed\xfa\xcf" | b"\xfe\xed\xfa\xce" => {
                u32::from_be_bytes(payload[4..8].try_into().unwrap())
            }
            _ => return Err(invalid("pack-shell is not a thin Mach-O executable")),
        };
        return Ok(match cpu_type {
            7 => "x86".to_string(),
            0x01000007 => "x86_64".to_string(),
            0x0100000c => "aarch64".to_string(),
            _ => format!("macho-cpu-{cpu_type:#x}"),
        });
    }

    if payload.len() < 20 || &payload[..4] != b"\x7fELF" {
        return Err(invalid("pack-shell is not an ELF executable"));
    }
    let machine_bytes: [u8; 2] = payload[18..20].try_into().unwrap();
    let machine = match payload.get(5) {
        Some(1) => u16::from_le_bytes(machine_bytes),
        Some(2) => u16::from_be_bytes(machine_bytes),
        _ => return Err(invalid("pack-shell ELF header has an invalid byte order")),
    };
    Ok(match machine {
        3 => "x86".to_string(),
        62 => "x86_64".to_string(),
        183 => "aarch64".to_string(),
        _ => format!("elf-machine-{machine:#x}"),
    })
}

fn same_file_identity(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    if before.len() != after.len() || before.modified().ok() != after.modified().ok() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        before.dev() == after.dev() && before.ino() == after.ino()
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn read_verified_pack_shell(path: &Path, target: &str) -> io::Result<(Vec<u8>, fs::Permissions)> {
    let before = fs::symlink_metadata(path)?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "pack-shell binary must be a regular non-symlink: {}",
                path.display()
            ),
        ));
    }
    let mut source = fs::File::open(path)?;
    let opened = source.metadata()?;
    let mut payload = Vec::new();
    source.read_to_end(&mut payload)?;
    let after = fs::symlink_metadata(path)?;
    if !same_file_identity(&before, &opened) || !same_file_identity(&opened, &after) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "pack-shell binary changed while being staged: {}",
                path.display()
            ),
        ));
    }
    let actual = pack_shell_binary_architecture(&payload, target)?;
    let expected = expected_pack_shell_architecture(target);
    if actual != expected {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("pack-shell architecture mismatch: expected {expected}, got {actual}"),
        ));
    }
    Ok((payload, opened.permissions()))
}

fn pack_shell_binary_name(target: &str) -> &'static str {
    if target.contains("windows") || target.ends_with("-msvc") {
        "pack-shell.exe"
    } else {
        "pack-shell"
    }
}

#[cfg(not(target_os = "macos"))]
fn reset_dir(path: &Path) -> io::Result<()> {
    if path.exists() {
        clear_dir(path)?;
    } else {
        fs::create_dir_all(path)?;
    }
    Ok(())
}

fn reset_staged_runtime(path: &Path) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        return reset_staged_runtime_macos(path);
    }
    #[cfg(not(target_os = "macos"))]
    {
        reset_dir(path)
    }
}

#[cfg(not(target_os = "macos"))]
fn clear_dir(path: &Path) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let entry_path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            clear_dir(&entry_path)?;
            fs::remove_dir(&entry_path)?;
        } else {
            fs::remove_file(&entry_path)?;
        }
    }
    Ok(())
}

fn copy_file(src: &Path, dst: &Path) -> io::Result<u64> {
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)?;
    }
    let bytes = fs::copy(src, dst)?;
    if let Ok(permissions) = fs::metadata(src).map(|metadata| metadata.permissions()) {
        let _ = fs::set_permissions(dst, permissions);
    }
    Ok(bytes)
}

fn copy_development_venv_tree(src: &Path, dst: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let source_path = entry.path();
        let destination_path = dst.join(entry.file_name());
        let metadata = fs::symlink_metadata(&source_path)?;
        if metadata.file_type().is_symlink() {
            let resolved = fs::canonicalize(&source_path)?;
            if !resolved.is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("development venv symlink is not a file: {}", source_path.display()),
                ));
            }
            copy_file(&resolved, &destination_path)?;
        } else if metadata.is_dir() {
            copy_development_venv_tree(&source_path, &destination_path)?;
        } else if metadata.is_file() {
            copy_file(&source_path, &destination_path)?;
        } else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("development venv contains an unsupported entry: {}", source_path.display()),
            ));
        }
    }
    Ok(())
}

fn write_development_runtime_path(venv_root: &Path) -> io::Result<()> {
    let unix_site_packages = venv_root.join("lib/python3.13/site-packages");
    let windows_site_packages = venv_root.join("Lib/site-packages");
    let site_packages = if unix_site_packages.is_dir() {
        unix_site_packages
    } else if windows_site_packages.is_dir() {
        windows_site_packages
    } else {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("development venv site-packages not found below {}", venv_root.display()),
        ));
    };
    for entry in fs::read_dir(&site_packages)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with("__editable__.tobkiri_runtime-") && name.ends_with(".pth") {
            fs::remove_file(entry.path())?;
        }
    }
    fs::write(
        site_packages.join("tobkiri_staged_runtime.pth"),
        "import os,sys; sys.path.insert(0, os.path.dirname(sys.prefix))\n",
    )
}

fn copy_tracked_runtime_tree(repo_root: &Path, staged_root: &Path) -> io::Result<bool> {
    let git = packaging_toolchain::verified_tool("git")?;
    let output = match git
        .command()?
        .args(["ls-files", "-z", "--", APP_SOURCE_DIR])
        .current_dir(repo_root)?
        .output()
    {
        Ok(output) => output,
        Err(_) => return Ok(false),
    };

    if !output.status.success() {
        return Ok(false);
    }

    let source_prefix = format!("{APP_SOURCE_DIR}/");
    for rel in output.stdout.split(|byte| *byte == 0) {
        if rel.is_empty() {
            continue;
        }
        let rel = String::from_utf8_lossy(rel);
        let Some(rel_under_app) = rel.strip_prefix(&source_prefix) else {
            continue;
        };
        let rel_path = Path::new(rel_under_app);
        if should_skip(rel_path, false) {
            continue;
        }
        let source_path = repo_root.join(rel.as_ref());
        let metadata = match fs::symlink_metadata(&source_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if metadata.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("tracked runtime source may not be a symlink: {}", rel),
            ));
        }
        if !metadata.is_file() {
            continue;
        }
        copy_file(&source_path, &staged_root.join(rel_path))?;
    }

    Ok(true)
}

fn configured_panel_build_dir(project_dir: &Path) -> Option<PathBuf> {
    let raw = std::env::var_os(PANEL_BUILD_DIR_ENV)?;
    let configured = PathBuf::from(raw);
    if configured.as_os_str().is_empty() {
        return None;
    }
    Some(if configured.is_absolute() {
        configured
    } else {
        project_dir
            .parent()
            .map(|launcher_root| launcher_root.join("frontend").join(&configured))
            .unwrap_or(configured)
    })
}

fn copy_generated_resource_dirs(
    project_dir: &Path,
    runtime_root: &Path,
    staged_root: &Path,
    sealed_python_source: Option<&Path>,
) -> io::Result<()> {
    let configured_panel_dir = configured_panel_build_dir(project_dir);
    for rel_dir in GENERATED_RESOURCE_DIRS {
        let source_dir = if *rel_dir == SEALED_PYTHON_ROOT {
            let Some(source) = sealed_python_source else {
                continue;
            };
            source.to_path_buf()
        } else if *rel_dir == PANEL_RESOURCE_DIR {
            configured_panel_dir
                .clone()
                .unwrap_or_else(|| runtime_root.join(rel_dir))
        } else {
            runtime_root.join(rel_dir)
        };
        if !source_dir.exists() {
            if *rel_dir == PANEL_RESOURCE_DIR && configured_panel_dir.is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!(
                        "configured panel build directory is missing: {}",
                        source_dir.display()
                    ),
                ));
            }
            continue;
        }
        if fs::symlink_metadata(&source_dir)?.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "generated runtime resource may not be a symlink: {}",
                    source_dir.display()
                ),
            ));
        }
        if *rel_dir == SEALED_PYTHON_ROOT {
            copy_dir_recursive(&source_dir, &staged_root.join(rel_dir))?;
            mirror_directory_permissions(&source_dir, &staged_root.join(rel_dir))?;
        } else {
            copy_dir_recursive_filtered(&source_dir, &staged_root.join(rel_dir), runtime_root)?;
        }
    }
    Ok(())
}

fn configured_sealed_python_snapshot() -> io::Result<PathBuf> {
    let configured = std::env::var_os(PACKAGING_PYTHON_SNAPSHOT_ENV).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            format!("{PACKAGING_PYTHON_SNAPSHOT_ENV} is required"),
        )
    })?;
    let source = PathBuf::from(configured);
    if !source.is_absolute() || source.canonicalize()? != source {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "sealed Python snapshot path is not canonical absolute",
        ));
    }
    Ok(source)
}

fn copy_dir_recursive(src: &Path, dst: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let source_path = entry.path();
        let target_path = dst.join(entry.file_name());
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "symlinked presentation release entry is not accepted: {}",
                    source_path.display()
                ),
            ));
        }
        if file_type.is_dir() {
            copy_dir_recursive(&source_path, &target_path)?;
        } else if file_type.is_file() {
            copy_file(&source_path, &target_path)?;
        } else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "unsupported presentation release entry: {}",
                    source_path.display()
                ),
            ));
        }
    }
    Ok(())
}

fn mirror_directory_permissions(src: &Path, dst: &Path) -> io::Result<()> {
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            mirror_directory_permissions(&entry.path(), &dst.join(entry.file_name()))?;
        }
    }
    fs::set_permissions(dst, fs::metadata(src)?.permissions())
}

fn copy_dir_recursive_filtered(src: &Path, dst: &Path, runtime_root: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let source_path = entry.path();
        let target_path = dst.join(entry.file_name());
        let file_type = entry.file_type()?;
        let relative = source_path
            .strip_prefix(runtime_root)
            .unwrap_or(&source_path);

        if should_skip(relative, file_type.is_dir()) {
            continue;
        }

        if file_type.is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "generated runtime resource may not be a symlink: {}",
                    source_path.display()
                ),
            ));
        }

        if file_type.is_dir() {
            copy_dir_recursive_filtered(&source_path, &target_path, runtime_root)?;
        } else if file_type.is_file() {
            copy_file(&source_path, &target_path)?;
        }
    }
    Ok(())
}

fn should_skip(relative: &Path, is_dir: bool) -> bool {
    let Some(first) = relative.components().next().map(|c| c.as_os_str()) else {
        return false;
    };

    let first = first.to_str();
    if matches!(
        first,
        Some(".env")
            | Some(".env.local")
            | Some(".backups")
            | Some(".backup_dead_code_removal")
            | Some("chats")
            | Some("tenpu")
            | Some("tests")
            | Some("user_data")
            | Some("userdata")
            | Some("venv")
    ) {
        return true;
    }

    if matches!(
        first,
        Some(".git")
            | Some(".mypy_cache")
            | Some(".pytest_cache")
            | Some(".ruff_cache")
            | Some(".rumi_snapshots")
            | Some(".venv")
            | Some("docs")
    ) {
        return true;
    }

    if relative.components().any(|component| {
        matches!(
            component.as_os_str().to_str(),
            Some("__pycache__")
                | Some(".pytest_cache")
                | Some(".ruff_cache")
                | Some(".rumi_snapshots")
                | Some(".venv")
                | Some("node_modules")
                | Some("target")
                | Some("user_data")
                | Some("userdata")
        )
    }) {
        return true;
    }

    if !is_dir {
        if relative.file_name().and_then(|name| name.to_str()) == Some(".DS_Store") {
            return true;
        }
        if matches!(
            relative.extension().and_then(|ext| ext.to_str()),
            Some("bak") | Some("pyc") | Some("pyo") | Some("zip")
        ) {
            return true;
        }
    }

    if first == Some("frontend") {
        let second = relative.components().nth(1).map(|c| c.as_os_str());
        if matches!(
            second.and_then(|part| part.to_str()),
            Some("node_modules") | Some(".vite-temp")
        ) {
            return true;
        }

        if !is_dir
            && matches!(
                relative.extension().and_then(|ext| ext.to_str()),
                Some("tsbuildinfo")
            )
        {
            return true;
        }
    }

    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsString;
    use std::sync::{Mutex, MutexGuard};
    use std::time::{SystemTime, UNIX_EPOCH};

    static ENVIRONMENT_LOCK: Mutex<()> = Mutex::new(());

    fn environment_lock() -> MutexGuard<'static, ()> {
        ENVIRONMENT_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    struct TestTree {
        root: PathBuf,
    }

    impl TestTree {
        fn new(label: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock must be after the Unix epoch")
                .as_nanos();
            // macOS commonly exposes its temporary directory through the
            // `/var` alias for canonical `/private/var`. Resolve only this
            // trusted OS-provided base before adding fixture-controlled names;
            // production target roots remain subject to strict symlink checks.
            let temp_base = std::env::temp_dir()
                .canonicalize()
                .expect("system temporary directory should canonicalize");
            let root = temp_base.join(format!(
                "tobkiri-build-script-{label}-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir_all(&root).expect("test tree should be creatable");
            Self { root }
        }

        fn path(&self) -> &Path {
            &self.root
        }
    }

    impl Drop for TestTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn test_tree_resolves_only_the_trusted_system_temp_base() {
        let tree = TestTree::new("canonical-temp-base");
        assert_eq!(
            tree.path(),
            tree.path()
                .canonicalize()
                .expect("fixture path should remain canonical")
        );
    }

    #[test]
    fn formal_defaults_packaging_contract_has_typed_fixed_argv() {
        let projection = DefaultsPackagingProjection {
            source_artifact: Path::new("/trusted/source artifact"),
            bundle_root: Path::new("/staging/bundle"),
            artifact_root: Path::new("/staging/artifacts"),
            relative_path: "platform-artifacts/Tobkiri.app",
            entrypoint: "Contents/MacOS/Tobkiri",
            platform: "macos",
            architecture: "arm64",
            bundle_identity: "io.tobkiri.test",
        };
        assert_eq!(
            FORMAL_DEFAULTS_PACKAGING_COMMAND,
            "tobkiri-core-package-defaults-v1"
        );
        assert_eq!(
            projection.argv(),
            vec![
                "--source-artifact",
                "/trusted/source artifact",
                "--bundle-root",
                "/staging/bundle",
                "--artifact-root",
                "/staging/artifacts",
                "--relative-path",
                "platform-artifacts/Tobkiri.app",
                "--entrypoint",
                "Contents/MacOS/Tobkiri",
                "--platform",
                "macos",
                "--architecture",
                "arm64",
                "--bundle-identity",
                "io.tobkiri.test",
                "--source-provenance-file",
                "packaging-source-provenance.v1.json",
            ]
            .into_iter()
            .map(OsString::from)
            .collect::<Vec<_>>()
        );
    }

    #[test]
    fn formal_defaults_packaging_contract_never_exports_snapshot_paths() {
        assert!(!ISOLATED_ENVIRONMENT_KEYS
            .iter()
            .any(|key| key.contains("SOURCE") || key.contains("PROVENANCE")));
        assert_eq!(
            Path::new("packaging-source-provenance.v1.json")
                .components()
                .count(),
            1
        );
    }

    #[test]
    fn formal_defaults_packaging_cleanup_requires_confirmed_reap() {
        #[derive(Clone, Copy)]
        enum Lifecycle {
            NoChild,
            Reaped,
            RunningUncontained,
        }
        let cleanup_authorized = |result| matches!(result, Lifecycle::NoChild | Lifecycle::Reaped);
        assert!(cleanup_authorized(Lifecycle::NoChild));
        assert!(cleanup_authorized(Lifecycle::Reaped));
        assert!(!cleanup_authorized(Lifecycle::RunningUncontained));
    }

    #[test]
    fn core_build_stage_distinguishes_shell_from_final_application() {
        let shell_config = serde_json::json!({
            "identifier": "io.tobkiri.shell.tauri",
            "mainBinaryName": "tobkiri-shell",
        });
        let final_config = serde_json::json!({
            "identifier": "io.tobkiri.launcher",
            "mainBinaryName": "tobkiri-launcher",
        });
        let classify = |value: &serde_json::Value| {
            if value["identifier"] == "io.tobkiri.shell.tauri"
                && value["mainBinaryName"] == "tobkiri-shell"
            {
                CoreBuildStage::IntermediateShell
            } else {
                CoreBuildStage::FinalApplication
            }
        };
        assert_eq!(classify(&shell_config), CoreBuildStage::IntermediateShell);
        assert_eq!(classify(&final_config), CoreBuildStage::FinalApplication);
    }

    #[test]
    fn production_stage_does_not_require_external_release_root() {
        let source = include_str!("build.rs");
        let production_gate = source
            .split("fn stage_presentation_release(")
            .nth(1)
            .expect("stage function should exist");
        assert!(production_gate.contains("produce_and_stage_core_presentation_release"));
        assert!(!production_gate.contains("production package requires"));
    }

    #[test]
    fn runtime_manifest_uses_exact_portable_full_tree_order() {
        let tree = TestTree::new("runtime-manifest-order");
        for relative in [
            "bootstrap/00_env_check.py",
            "bootstrap.py",
            "lib/i18n/index.ts",
            "lib/i18n.test.ts",
        ] {
            let path = tree.path().join(relative);
            fs::create_dir_all(path.parent().expect("fixture file should have a parent"))
                .expect("fixture directory should be creatable");
            fs::write(path, relative.as_bytes()).expect("fixture file should be writable");
        }

        write_runtime_resource_manifest(tree.path()).expect("manifest should be writable");
        let manifest: serde_json::Value = serde_json::from_slice(
            &fs::read(tree.path().join(RUNTIME_RESOURCE_MANIFEST))
                .expect("manifest should be readable"),
        )
        .expect("manifest should be valid JSON");
        let paths = manifest["entries"]
            .as_array()
            .expect("manifest entries should be an array")
            .iter()
            .map(|entry| {
                entry["path"]
                    .as_str()
                    .expect("entry path should be a string")
            })
            .collect::<Vec<_>>();
        assert_eq!(
            paths,
            [
                "bootstrap.py",
                "bootstrap/00_env_check.py",
                "lib/i18n.test.ts",
                "lib/i18n/index.ts",
            ]
        );
    }

    #[test]
    fn runtime_manifest_rejects_case_unicode_and_hardlink_ambiguity() {
        let upper = runtime_resource_paths::CanonicalResourcePath::parse("Entry.py").unwrap();
        let lower = runtime_resource_paths::CanonicalResourcePath::parse("entry.py").unwrap();
        let mut ambiguity_keys = std::collections::BTreeSet::new();
        assert!(ambiguity_keys.insert(upper.ambiguity_key()));
        assert!(!ambiguity_keys.insert(lower.ambiguity_key()));

        let unicode_tree = TestTree::new("runtime-manifest-unicode");
        fs::write(unicode_tree.path().join("é.py"), b"unicode\n").unwrap();
        assert!(write_runtime_resource_manifest(unicode_tree.path()).is_err());

        #[cfg(unix)]
        {
            let hardlink_tree = TestTree::new("runtime-manifest-hardlink");
            let outside = hardlink_tree.path().join("outside.py");
            fs::write(&outside, b"shared\n").unwrap();
            fs::create_dir_all(hardlink_tree.path().join("app")).unwrap();
            fs::hard_link(&outside, hardlink_tree.path().join("app/entry.py")).unwrap();
            assert!(write_runtime_resource_manifest(hardlink_tree.path()).is_err());
        }
    }

    #[test]
    fn sealed_python_binding_accepts_exact_tree_and_rejects_domain_swap() {
        let _environment = environment_lock();
        let target = if cfg!(target_arch = "aarch64") {
            "aarch64-apple-darwin"
        } else {
            "x86_64-apple-darwin"
        };
        let _target = EnvironmentGuard::set_value("TARGET", target);
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");
        let tree = TestTree::new("sealed-python");
        let root = tree.path().join(SEALED_PYTHON_ROOT);
        let required = [
            "app/defaultspack_entry.py",
            "app/host_helper_entry.py",
            "app/kernel_entry.py",
            "lease.v1",
            SEALED_PYTHON_DIRECTORY_MODES,
            "sentinels/native.sha256",
            "sentinels/site-packages.sha256",
            "sentinels/stdlib.sha256",
            "venv/bin/python3",
            "venv/lib/python3.13/site-packages/tobkiri_sealed/bootstrap.py",
        ];
        let mut directory_paths = std::collections::BTreeSet::new();
        for relative in required {
            let mut parent = Path::new(relative).parent();
            while let Some(directory) = parent.filter(|path| !path.as_os_str().is_empty()) {
                directory_paths.insert(portable_relative_path(directory));
                parent = directory.parent();
            }
        }
        let directory_modes = serde_json::to_string_pretty(&serde_json::json!({
            "schema": SEALED_PYTHON_DIRECTORY_MODES_SCHEMA,
            "directories": std::iter::once(serde_json::json!({"path": ".", "mode": "0555"}))
                .chain(directory_paths.iter().map(|path| serde_json::json!({"path": path, "mode": "0555"})))
                .collect::<Vec<_>>()
        }))
        .unwrap();
        let mut files = Vec::new();
        for relative in required {
            let path = root.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let payload = if relative == SEALED_PYTHON_DIRECTORY_MODES {
                directory_modes.clone()
            } else if relative.ends_with("tobkiri_sealed/bootstrap.py") {
                sealed_python_protocol::REQUIRED_TEMPLATE_FRAGMENTS.join("\n")
                    + "\nparse_known_args role_args chmod\n"
            } else {
                relative.to_string()
            };
            fs::write(&path, payload.as_bytes()).unwrap();
            let executable = relative == "venv/bin/python3";
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(
                    &path,
                    fs::Permissions::from_mode(if executable { 0o555 } else { 0o444 }),
                )
                .unwrap();
            }
            files.push(serde_json::json!({
                "path": relative,
                "size": payload.len(),
                "sha256": raw_byte_digest(payload.as_bytes()),
                "executable": executable
            }));
        }
        let inventory_digest = sealed_python_inventory_digest(&files).unwrap();
        let manifest = serde_json::json!({
            "schema": SEALED_PYTHON_SCHEMA,
            "environment_digest": inventory_digest,
            "platform": "macos",
            "architecture": expected_sealed_python_target(target).unwrap().1,
            "python_version": "3.13.13",
            "package_provenance": {
                "kind": "pinned-python-build-standalone-v1",
                "package_id": "dev.rumiai.app",
                "release_digest": raw_byte_digest(b"release")
            },
            "sentinels": {
                "stdlib_sha256": raw_byte_digest(b"stdlib"),
                "site_packages_sha256": raw_byte_digest(b"site"),
                "native_sha256": raw_byte_digest(b"native")
            },
            "files": files
        });
        let manifest_bytes = serde_json::to_vec(&manifest).unwrap();
        fs::write(root.join(SEALED_PYTHON_MANIFEST), &manifest_bytes).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(SEALED_PYTHON_MANIFEST),
                fs::Permissions::from_mode(0o444),
            )
            .unwrap();
            for relative in &directory_paths {
                fs::set_permissions(root.join(relative), fs::Permissions::from_mode(0o555))
                    .unwrap();
            }
            fs::set_permissions(&root, fs::Permissions::from_mode(0o555)).unwrap();
        }
        let inventory_digest = raw_byte_digest(&manifest_bytes);
        let _inventory =
            EnvironmentGuard::set_value(PACKAGING_PYTHON_INVENTORY_SHA_ENV, &inventory_digest);
        bind_sealed_python_root(&root, true).unwrap();
        let staged = tree.path().join("staged");
        copy_generated_resource_dirs(
            &tree.path().join("tobkiri_launcher/src-tauri"),
            &tree.path().join(APP_SOURCE_DIR),
            &staged,
            Some(&root),
        )
        .unwrap();
        bind_sealed_python_root(&staged.join(SEALED_PYTHON_ROOT), true).unwrap();
        sealed_python_reseal_work_budget(&staged.join(SEALED_PYTHON_ROOT), &inventory_digest)
            .expect("digest-bound staged inventory should authorize a re-seal work budget");

        let mut swapped = manifest.clone();
        swapped["environment_digest"] = swapped["package_provenance"]["release_digest"].clone();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(SEALED_PYTHON_MANIFEST),
                fs::Permissions::from_mode(0o644),
            )
            .unwrap();
        }
        fs::write(
            root.join(SEALED_PYTHON_MANIFEST),
            serde_json::to_vec(&swapped).unwrap(),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(SEALED_PYTHON_MANIFEST),
                fs::Permissions::from_mode(0o444),
            )
            .unwrap();
        }
        assert!(bind_sealed_python_root(&root, true).is_err());
        assert!(sealed_python_reseal_work_budget(&root, &inventory_digest).is_err());

        let mut prefixed = manifest;
        prefixed["sentinels"]["stdlib_sha256"] =
            serde_json::Value::String(format!("sha256:{}", raw_byte_digest(b"stdlib")));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                root.join(SEALED_PYTHON_MANIFEST),
                fs::Permissions::from_mode(0o644),
            )
            .unwrap();
        }
        fs::write(
            root.join(SEALED_PYTHON_MANIFEST),
            serde_json::to_vec(&prefixed).unwrap(),
        )
        .unwrap();
        assert!(bind_sealed_python_root(&root, true).is_err());
    }

    #[test]
    fn sealed_python_target_domain_is_derived_from_exact_cargo_target() {
        assert_eq!(
            expected_sealed_python_target("aarch64-apple-darwin").unwrap(),
            ("macos", "arm64")
        );
        assert_eq!(
            expected_sealed_python_target("x86_64-apple-darwin").unwrap(),
            ("macos", "x86_64")
        );
        assert_eq!(
            expected_sealed_python_target("x86_64-pc-windows-msvc").unwrap(),
            ("windows", "x86_64")
        );
        assert_eq!(
            expected_sealed_python_target("x86_64-unknown-linux-gnu").unwrap(),
            ("linux", "x86_64")
        );
        assert_eq!(
            expected_pack_shell_architecture("aarch64-apple-darwin"),
            "aarch64"
        );
        for unsupported in [
            "arm64-apple-darwin",
            "aarch64-unknown-linux-gnu",
            "x86_64-apple-darwin-extra",
        ] {
            assert_eq!(
                expected_sealed_python_target(unsupported)
                    .expect_err("unknown target must fail closed")
                    .kind(),
                io::ErrorKind::Unsupported
            );
        }
    }

    #[test]
    fn sealed_python_digest_matches_python_fixed_field_framing() {
        let files = vec![serde_json::json!({
            "executable": false,
            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "size": 1,
            "path": "a.txt"
        })];
        assert_eq!(
            sealed_python_inventory_digest(&files).unwrap(),
            "2cb00f58e1f3c0794078cb2a0580641e7941aa509e09e3651cbd654c9a32fab2"
        );
        let reordered = vec![serde_json::from_str(
            r#"{"size":1,"executable":false,"path":"a.txt","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
        )
        .unwrap()];
        assert_eq!(
            sealed_python_inventory_digest(&reordered).unwrap(),
            "2cb00f58e1f3c0794078cb2a0580641e7941aa509e09e3651cbd654c9a32fab2"
        );
        let mut two_files = files.clone();
        two_files.push(serde_json::json!({
            "path": "b.txt",
            "size": 2,
            "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "executable": true
        }));
        let original = sealed_python_inventory_digest(&two_files).unwrap();
        two_files.reverse();
        assert_ne!(
            sealed_python_inventory_digest(&two_files).unwrap(),
            original
        );
    }

    #[test]
    fn non_macos_release_targets_are_rejected_before_packaging() {
        let _environment = environment_lock();
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");
        for target in ["x86_64-pc-windows-msvc", "x86_64-unknown-linux-gnu"] {
            let _target = EnvironmentGuard::set_value("TARGET", target);
            let error = reject_unsupported_sealed_python_release_target().unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::Unsupported);
            assert!(error.to_string().contains(target));
        }
    }

    struct EnvironmentGuard {
        key: &'static str,
        previous: Option<OsString>,
    }

    impl EnvironmentGuard {
        fn set_value(key: &'static str, value: &str) -> Self {
            let previous = std::env::var_os(key);
            std::env::set_var(key, value);
            Self { key, previous }
        }

        fn set_path(key: &'static str, value: &Path) -> Self {
            let previous = std::env::var_os(key);
            std::env::set_var(key, value);
            Self { key, previous }
        }

        fn clear(key: &'static str) -> Self {
            let previous = std::env::var_os(key);
            std::env::remove_var(key);
            Self { key, previous }
        }
    }

    impl Drop for EnvironmentGuard {
        fn drop(&mut self) {
            if let Some(value) = &self.previous {
                std::env::set_var(self.key, value);
            } else {
                std::env::remove_var(self.key);
            }
        }
    }

    #[test]
    fn macos_artifact_policy_separates_production_and_ci_identity_domains() {
        let _environment = environment_lock();
        let _target = EnvironmentGuard::set_value("TARGET", "aarch64-apple-darwin");
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");

        {
            let _policy = EnvironmentGuard::set_value(MACOS_ARTIFACT_POLICY_ENV, "ci-e2e-v1");
            let _certificate =
                EnvironmentGuard::set_value(MACOS_CI_CERT_SHA256_ENV, &"a".repeat(64));
            let _public_key =
                EnvironmentGuard::set_value(MACOS_CI_PUBLIC_KEY_ENV, &BASE64.encode([7_u8; 32]));
            let _team = EnvironmentGuard::clear(APPLE_TEAM_ID_ENV);
            bind_macos_artifact_policy().unwrap();
        }
        {
            let _policy = EnvironmentGuard::set_value(MACOS_ARTIFACT_POLICY_ENV, "production-v1");
            let _certificate = EnvironmentGuard::clear(MACOS_CI_CERT_SHA256_ENV);
            let _public_key = EnvironmentGuard::clear(MACOS_CI_PUBLIC_KEY_ENV);
            let _team = EnvironmentGuard::clear(APPLE_TEAM_ID_ENV);
            bind_macos_artifact_policy().unwrap();
        }
    }

    #[test]
    fn macos_artifact_policy_rejects_ad_hoc_and_cross_domain_inputs() {
        let _environment = environment_lock();
        let _target = EnvironmentGuard::set_value("TARGET", "aarch64-apple-darwin");
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");

        for policy in ["ad-hoc", "local", "ci"] {
            let _policy = EnvironmentGuard::set_value(MACOS_ARTIFACT_POLICY_ENV, policy);
            assert!(bind_macos_artifact_policy().is_err());
        }
        {
            let _policy = EnvironmentGuard::set_value(MACOS_ARTIFACT_POLICY_ENV, "production-v1");
            let _certificate = EnvironmentGuard::clear(MACOS_CI_CERT_SHA256_ENV);
            let _public_key = EnvironmentGuard::clear(MACOS_CI_PUBLIC_KEY_ENV);
            let _team = EnvironmentGuard::set_value(APPLE_TEAM_ID_ENV, "ABC1234567");
            assert!(bind_macos_artifact_policy().is_err());
        }
        {
            let _policy = EnvironmentGuard::set_value(MACOS_ARTIFACT_POLICY_ENV, "ci-e2e-v1");
            let _certificate =
                EnvironmentGuard::set_value(MACOS_CI_CERT_SHA256_ENV, &"c".repeat(64));
            let _public_key =
                EnvironmentGuard::set_value(MACOS_CI_PUBLIC_KEY_ENV, &BASE64.encode([8_u8; 32]));
            let _team = EnvironmentGuard::set_value(APPLE_TEAM_ID_ENV, "ABC1234567");
            assert!(bind_macos_artifact_policy().is_err());
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn production_source_provenance_uses_versioned_head_blob_without_local_config() {
        let _environment = environment_lock();
        let git = Path::new("/Library/Developer/CommandLineTools/usr/bin/git");
        assert!(git.is_file(), "formal Command Line Tools Git is required");
        let digest = format!("{:x}", Sha256::digest(fs::read(git).unwrap()));
        let _git_path = EnvironmentGuard::set_path(packaging_toolchain::GIT_PATH_ENV, git);
        let _git_digest = EnvironmentGuard::set_value(packaging_toolchain::GIT_SHA256_ENV, &digest);
        let (identity, revision) =
            current_source_provenance().expect("versioned HEAD provenance should resolve");
        let (_, authority_digest) = identity
            .split_once("@sha256:")
            .expect("source identity should bind its authority blob digest");
        assert!(identity.starts_with("github:"));
        assert_eq!(authority_digest.len(), 64);
        assert!(authority_digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit()));
        assert_eq!(revision.len(), 40);
    }

    #[test]
    fn generated_untracked_allowlist_is_exact_and_type_checked() {
        let tree = TestTree::new("generated-untracked-allowlist");
        let allowed_root = tree.path().join("pack-shell/target");
        fs::create_dir_all(&allowed_root).unwrap();
        fs::write(allowed_root.join("artifact"), b"generated").unwrap();
        verify_allowed_generated_untracked(tree.path(), "pack-shell/target/artifact")
            .expect("exact generated output should be allowed");

        let cargo = tree.path().join(".cargo");
        fs::create_dir_all(&cargo).unwrap();
        fs::write(cargo.join("config.toml"), b"[build]\nrustc='attacker'\n").unwrap();
        verify_allowed_generated_untracked(tree.path(), ".cargo/config.toml")
            .expect_err("authority-affecting untracked config must be rejected");

        for forbidden in [".pythonrc.py", "sitecustomize.py", "tool-wrapper"] {
            fs::write(tree.path().join(forbidden), b"attacker").unwrap();
            verify_allowed_generated_untracked(tree.path(), forbidden)
                .expect_err("startup and wrapper injection must be rejected");
        }

        let gen_root = tree.path().join("tobkiri_launcher/src-tauri/gen/app");
        fs::create_dir_all(&gen_root).unwrap();
        fs::write(gen_root.join("runtime.py"), b"generated runtime").unwrap();
        verify_allowed_generated_untracked(
            tree.path(),
            "tobkiri_launcher/src-tauri/gen/app/runtime.py",
        )
        .expect("regular generated runtime output should be allowed");

        let sibling = tree.path().join("tobkiri_launcher/src-tauri/gen-evil");
        fs::create_dir_all(&sibling).unwrap();
        fs::write(sibling.join("runtime.py"), b"attacker").unwrap();
        verify_allowed_generated_untracked(
            tree.path(),
            "tobkiri_launcher/src-tauri/gen-evil/runtime.py",
        )
        .expect_err("generated-root sibling must be rejected");

        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            use std::os::unix::net::UnixListener;

            symlink("runtime.py", gen_root.join("linked.py")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/src-tauri/gen/app/linked.py",
            )
            .expect_err("generated symlink must be rejected");

            fs::hard_link(gen_root.join("runtime.py"), gen_root.join("hardlinked.py")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/src-tauri/gen/app/hardlinked.py",
            )
            .expect_err("generated hardlink must be rejected");

            // Keep the AF_UNIX fixture below macOS SUN_LEN even when the
            // checkout itself has a long temporary path.  The generated-root
            // allowlist contract, not this pathname length limit, is under
            // test.  This short root is still an explicit test-owned
            // directory; it is removed immediately after verification.
            let special_repository_root =
                PathBuf::from("/tmp").join(format!("tobkiri-generated-{}", std::process::id()));
            let special_root = special_repository_root.join("pack-shell/target");
            fs::create_dir_all(&special_root).unwrap();
            let special = special_root.join("device-like");
            let _listener = UnixListener::bind(&special).unwrap();
            verify_allowed_generated_untracked(
                &special_repository_root,
                "pack-shell/target/device-like",
            )
            .expect_err("generated special file must be rejected");
            fs::remove_dir_all(&special_repository_root).unwrap();

            let modules = tree.path().join("tobkiri_launcher/frontend/node_modules");
            let package_bin = modules.join("package/bin");
            fs::create_dir_all(&package_bin).unwrap();
            fs::write(package_bin.join("tool.js"), b"legitimate npm bin").unwrap();
            fs::create_dir_all(modules.join(".bin")).unwrap();
            fs::create_dir_all(modules.join("links")).unwrap();
            symlink("../package/bin/tool.js", modules.join("links/tool")).unwrap();
            symlink("../links/tool", modules.join(".bin/tool")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/tool",
            )
            .expect("bounded relative npm bin symlink chain should be allowed");

            symlink("/tmp/attacker", modules.join(".bin/absolute")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/absolute",
            )
            .expect_err("absolute npm symlink target must be rejected");

            symlink("../../../outside", modules.join(".bin/escape")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/escape",
            )
            .expect_err("npm symlink target outside node_modules must be rejected");

            symlink("../missing/tool", modules.join(".bin/broken")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/broken",
            )
            .expect_err("broken npm symlink must be rejected");

            symlink("cycle-b", modules.join(".bin/cycle-a")).unwrap();
            symlink("cycle-a", modules.join(".bin/cycle-b")).unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/cycle-a",
            )
            .expect_err("cyclic npm symlink chain must be rejected");

            let deep = modules.join("deep");
            fs::create_dir_all(&deep).unwrap();
            for index in 0..41 {
                let target = if index == 40 {
                    "../package/bin/tool.js".to_owned()
                } else {
                    format!("link-{}", index + 1)
                };
                symlink(target, deep.join(format!("link-{index}"))).unwrap();
            }
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/deep/link-0",
            )
            .expect_err("overlong npm symlink chain must be rejected");

            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/../package/bin/tool.js",
            )
            .expect_err("non-normalized generated path must be rejected");

            // Use a short explicit test root for the AF_UNIX fixture.  The
            // actual node_modules contract is still checked by the verifier,
            // while macOS SUN_LEN must not depend on the long temp checkout
            // path used by the other symlink cases.
            let short_repository_root =
                PathBuf::from("/tmp").join(format!("tobkiri-node-modules-{}", std::process::id()));
            let short_modules =
                short_repository_root.join("tobkiri_launcher/frontend/node_modules");
            let short_package_bin = short_modules.join("package/bin");
            fs::create_dir_all(&short_package_bin).unwrap();
            fs::create_dir_all(short_modules.join(".bin")).unwrap();
            let module_socket = short_package_bin.join("socket");
            let module_listener = UnixListener::bind(&module_socket).unwrap();
            symlink("../package/bin/socket", short_modules.join(".bin/socket")).unwrap();
            verify_allowed_generated_untracked(
                &short_repository_root,
                "tobkiri_launcher/frontend/node_modules/.bin/socket",
            )
            .expect_err("npm symlink to a special file must be rejected");
            drop(module_listener);
            fs::remove_dir_all(&short_repository_root).unwrap();

            let outside = tree.path().join("outside-hardlink");
            fs::write(&outside, b"attacker helper").unwrap();
            fs::hard_link(&outside, package_bin.join("hardlinked-helper")).unwrap();
            symlink(
                "../package/bin/hardlinked-helper",
                modules.join(".bin/hardlinked"),
            )
            .unwrap();
            verify_allowed_generated_untracked(
                tree.path(),
                "tobkiri_launcher/frontend/node_modules/.bin/hardlinked",
            )
            .expect_err("npm symlink to a hardlinked helper must be rejected");

            let symlinked_tree = TestTree::new("symlinked-gen-root");
            let replacement = symlinked_tree.path().join("replacement");
            fs::create_dir_all(&replacement).unwrap();
            fs::create_dir_all(symlinked_tree.path().join("tobkiri_launcher/src-tauri")).unwrap();
            symlink(
                &replacement,
                symlinked_tree.path().join("tobkiri_launcher/src-tauri/gen"),
            )
            .unwrap();
            verify_allowed_generated_untracked(
                symlinked_tree.path(),
                "tobkiri_launcher/src-tauri/gen",
            )
            .expect_err("generated runtime root symlink must remain forbidden");
        }
    }

    #[test]
    fn staged_gen_is_created_before_production_source_authority_checks() {
        let source = include_str!("build.rs");
        let stage = &source[source.find("fn stage_runtime_bundle").unwrap()
            ..source.find("fn collect_runtime_resource_files").unwrap()];
        assert!(
            stage.find("join(\"gen\").join(\"app\")").unwrap()
                < stage
                    .find("stage_presentation_release(&staged_root)")
                    .unwrap()
        );
        assert!(stage.contains("reset_staged_runtime(&staged_root)"));
        assert!(!stage.contains("reset_dir(&staged_root)"));
        let dispatcher = &source[source.find("fn stage_presentation_release(").unwrap()
            ..source.find("fn write_canonical_json(").unwrap()];
        assert!(dispatcher.contains("produce_and_stage_core_presentation_release"));
        let producer = &source[source
            .find("fn produce_and_stage_core_presentation_release")
            .unwrap()
            ..source.find("fn stage_presentation_release_at(").unwrap()];
        assert!(producer.contains("current_source_provenance()"));
        assert!(producer.contains("run_formal_defaults_packaging"));
        let verifier = &source[source
            .find("fn stage_presentation_release_from_snapshot")
            .unwrap()
            ..source.find("fn stage_core_verified_release(").unwrap()];
        assert!(verifier.contains("current_source_revision(&repository_root)"));
    }

    #[test]
    fn dev_runtime_preparation_completes_before_cargo_staging() {
        let config: serde_json::Value = serde_json::from_str(include_str!("tauri.conf.json"))
            .expect("Tauri configuration must remain valid JSON");
        let before_dev = config
            .get("build")
            .and_then(|build| build.get("beforeDevCommand"))
            .and_then(serde_json::Value::as_object)
            .expect("beforeDevCommand must use the waiting command form");

        assert_eq!(
            before_dev
                .get("script")
                .and_then(serde_json::Value::as_str),
            Some("cd ../frontend && node scripts/preflight-viewer-build.mjs && npm run build && cd ../.. && python tobkiri_launcher/scripts/prepare_viewer_runtime.py --mode dev")
        );
        assert_eq!(
            before_dev
                .get("wait")
                .and_then(serde_json::Value::as_bool),
            Some(true),
            "Cargo staging reads the generated control-panel/runtime resources, so Tauri must not run it concurrently with their preparation"
        );
    }

    #[test]
    fn only_debug_local_configuration_uses_the_unbundled_development_runtime() {
        let _environment = environment_lock();
        let _profile = EnvironmentGuard::set_value("PROFILE", "debug");
        let _config = EnvironmentGuard::set_value(
            "TAURI_CONFIG",
            r#"{
                "identifier":"dev.tobkiri.local-launcher",
                "bundle":{"resources":{}}
            }"#,
        );
        assert!(is_unbundled_local_development_build());

        let _production_profile = EnvironmentGuard::set_value("PROFILE", "release");
        assert!(
            !is_unbundled_local_development_build(),
            "release artifacts must retain sealed runtime staging"
        );
        drop(_production_profile);

        let _sealed_resource_config = EnvironmentGuard::set_value(
            "TAURI_CONFIG",
            r#"{
                "identifier":"dev.tobkiri.local-launcher",
                "bundle":{"resources":{"./gen/app":"app"}}
            }"#,
        );
        assert!(
            !is_unbundled_local_development_build(),
            "a configuration that packages the runtime must use sealed staging"
        );
    }

    #[test]
    fn final_generated_closure_is_resealed_before_runtime_manifest() {
        let source = include_str!("build.rs");
        let stage = &source[source.find("fn stage_runtime_bundle").unwrap()
            ..source.find("fn collect_runtime_resource_files").unwrap()];
        let generate = stage
            .find("stage_presentation_release(&staged_root)")
            .expect("final presentation generation must be explicit");
        let reseal = stage
            .find("rebase_staged_sealed_python(&staged_root)")
            .expect("sealed application closure must be rebuilt");
        let manifest = reseal
            + stage[reseal..]
                .find("write_runtime_resource_manifest(&staged_root)")
                .expect("outer runtime manifest must be final");
        assert!(generate < reseal && reseal < manifest);
        let rebase = &source[source.find("fn rebase_staged_sealed_python").unwrap()
            ..source.find("fn bind_sealed_python_root").unwrap()];
        assert!(rebase.contains("sealed_python_reseal_work_budget(&sealed_root, &expected)"));
        assert!(rebase.contains("command.output_with_budget(work_budget)"));
        assert!(!rebase.contains("command.output()?"));
    }

    #[test]
    fn core_producer_creates_owned_bundle_root_before_formal_generator() {
        let source = include_str!("build.rs");
        let producer = &source[source
            .find("fn produce_and_stage_core_presentation_release")
            .unwrap()
            ..source.find("fn stage_presentation_release_at(").unwrap()];
        let bundle_staging = producer
            .find("stage_core_defaults_bundle(&transaction, &repository_root)")
            .expect("Core producer must materialize its exact Defaults root first");
        let generator = producer
            .find("run_formal_defaults_packaging(")
            .expect("Core producer must use the formal generator");
        assert!(bundle_staging < generator);
        assert!(source.contains("fn stage_core_defaults_bundle("));
        assert!(source.contains("core_create_directory("));
        assert!(source.contains("copy_release_tree(&source, &bundle_root)"));
        assert!(source.contains(
            "let bound = core_open_relative(&transaction.root, \"release/ecosystem/defaultspack/v4\""
        ));
        assert!(producer.contains("Core transaction did not reach verified ownership"));
    }

    fn write_pack_shell_fixture(root: &Path, target: &str, profile: &str) -> PathBuf {
        let binary = root
            .join(target)
            .join(profile)
            .join(pack_shell_binary_name(target));
        fs::create_dir_all(binary.parent().expect("fixture binary has a parent"))
            .expect("fixture binary directory should be creatable");
        let architecture = expected_pack_shell_architecture(target);
        let mut payload = if target.contains("windows") {
            let mut payload = vec![0_u8; 128];
            payload[..2].copy_from_slice(b"MZ");
            payload[60..64].copy_from_slice(&64_u32.to_le_bytes());
            payload[64..68].copy_from_slice(b"PE\0\0");
            let machine = match architecture {
                "aarch64" => 0xaa64_u16,
                _ => 0x8664_u16,
            };
            payload[68..70].copy_from_slice(&machine.to_le_bytes());
            payload
        } else if target.contains("apple-darwin") {
            let cpu_type = match architecture {
                "aarch64" => 0x0100000c_u32,
                _ => 0x01000007_u32,
            };
            [b"\xcf\xfa\xed\xfe".as_slice(), &cpu_type.to_le_bytes()].concat()
        } else {
            let mut payload = vec![0_u8; 64];
            payload[..6].copy_from_slice(b"\x7fELF\x02\x01");
            let machine = match architecture {
                "aarch64" => 183_u16,
                _ => 62_u16,
            };
            payload[18..20].copy_from_slice(&machine.to_le_bytes());
            payload
        };
        payload.extend_from_slice(b"pack-shell fixture");
        fs::write(&binary, payload).expect("fixture binary should be writable");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&binary, fs::Permissions::from_mode(0o755))
                .expect("fixture binary should be executable");
        }
        binary
    }

    #[test]
    fn pack_shell_lookup_resolves_default_absolute_and_relative_target_dirs() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-target-dir");
        let target = "aarch64-apple-darwin";
        let profile = "release";
        let _target = EnvironmentGuard::set_value("TARGET", target);
        let _profile = EnvironmentGuard::set_value("PROFILE", profile);

        {
            let _target_dir = EnvironmentGuard::set_value(CARGO_TARGET_DIR_ENV, "");
            let binary = write_pack_shell_fixture(
                &tree.path().join("pack-shell").join("target"),
                target,
                profile,
            );
            assert_eq!(
                find_pack_shell_binary(tree.path()).expect("default lookup should succeed"),
                Some(binary.canonicalize().expect("fixture should canonicalize"))
            );
        }

        {
            let target_dir = tree.path().join("absolute-target");
            let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &target_dir);
            let binary = write_pack_shell_fixture(&target_dir, target, profile);
            assert_eq!(
                find_pack_shell_binary(tree.path()).expect("absolute lookup should succeed"),
                Some(binary.canonicalize().expect("fixture should canonicalize"))
            );
        }

        {
            let _target_dir =
                EnvironmentGuard::set_value(CARGO_TARGET_DIR_ENV, "relative target-雪");
            let binary =
                write_pack_shell_fixture(&tree.path().join("relative target-雪"), target, profile);
            assert_eq!(
                find_pack_shell_binary(tree.path()).expect("relative lookup should succeed"),
                Some(binary.canonicalize().expect("fixture should canonicalize"))
            );
        }
    }

    #[test]
    fn pack_shell_lookup_rejects_missing_wrong_and_traversing_binary_paths() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-invalid-paths");
        let target = "aarch64-apple-darwin";
        let _target = EnvironmentGuard::set_value("TARGET", target);
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");
        let target_dir = tree.path().join("target");
        let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &target_dir);

        write_pack_shell_fixture(&target_dir, target, "debug");
        assert!(find_pack_shell_binary(tree.path())
            .expect("wrong profile lookup should not error")
            .is_none());

        let invalid_target = EnvironmentGuard::set_value("TARGET", "../escape");
        let error = find_pack_shell_binary(tree.path())
            .expect_err("target path traversal must be rejected");
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        drop(invalid_target);
    }

    #[cfg(unix)]
    #[test]
    fn pack_shell_lookup_rejects_symlinked_binary() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-symlink");
        let target = "aarch64-apple-darwin";
        let _target = EnvironmentGuard::set_value("TARGET", target);
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");
        let target_dir = tree.path().join("target");
        let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &target_dir);
        let binary = target_dir
            .join(target)
            .join("release")
            .join(pack_shell_binary_name(target));
        fs::create_dir_all(binary.parent().expect("fixture binary has a parent"))
            .expect("fixture binary directory should be creatable");
        let outside = tree.path().join("outside-pack-shell");
        fs::write(&outside, b"outside fixture").expect("outside fixture should be writable");
        std::os::unix::fs::symlink(&outside, &binary).expect("binary symlink should be creatable");

        let error =
            find_pack_shell_binary(tree.path()).expect_err("symlinked binary must be rejected");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn cargo_target_dir_rejects_parent_traversal_and_file_root() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-target-root-invalid");
        {
            let _target_dir = EnvironmentGuard::set_value(CARGO_TARGET_DIR_ENV, "../outside");
            let error = resolve_cargo_target_dir(tree.path())
                .expect_err("parent traversal must be rejected");
            assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        }
        {
            let target_file = tree.path().join("target-file");
            fs::write(&target_file, b"not a directory").expect("target file should be writable");
            let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &target_file);
            let error = resolve_cargo_target_dir(tree.path())
                .expect_err("file target root must be rejected");
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        }
    }

    #[test]
    fn tauri_shell_target_uses_launcher_layout_and_validated_override() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("tauri-shell-target-layout");
        let project = tree.path().join("tobkiri_launcher/src-tauri");
        fs::create_dir_all(&project).expect("project fixture should exist");
        {
            let _target_dir = EnvironmentGuard::clear(CARGO_TARGET_DIR_ENV);
            assert_eq!(
                resolve_tauri_shell_target_dir(&project).expect("default target should resolve"),
                project
                    .canonicalize()
                    .expect("project should canonicalize")
                    .join("target")
            );
        }
        let target = "aarch64-apple-darwin";
        let override_root = tree.path().join("workflow-target");
        fs::create_dir_all(
            override_root
                .join(target)
                .join("release/bundle/macos/Tobkiri.app"),
        )
        .expect("workflow Shell layout should exist");
        let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &override_root);
        assert_eq!(
            resolve_tauri_shell_target_dir(&project)
                .expect("validated target override should resolve")
                .join(target)
                .join("release/bundle/macos/Tobkiri.app"),
            override_root
                .join(target)
                .join("release/bundle/macos/Tobkiri.app")
        );
    }

    #[cfg(unix)]
    #[test]
    fn cargo_target_dir_rejects_symlinked_root() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-target-root-symlink");
        let outside = tree.path().join("outside");
        fs::create_dir_all(&outside).expect("outside directory should be creatable");
        let target_link = tree.path().join("target-link");
        std::os::unix::fs::symlink(&outside, &target_link)
            .expect("target symlink should be creatable");
        let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &target_link);
        let error = resolve_cargo_target_dir(tree.path())
            .expect_err("symlinked target root must be rejected");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn cargo_target_dir_rejects_user_controlled_macos_var_alias() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-macos-var-alias");
        let alias = Path::new("/var");
        assert_ne!(
            alias
                .canonicalize()
                .expect("macOS /var alias should resolve"),
            alias
        );
        let target_dir = alias.join(format!("tobkiri-user-target-{}", std::process::id()));
        let _target_dir = EnvironmentGuard::set_path(CARGO_TARGET_DIR_ENV, &target_dir);

        let error = resolve_cargo_target_dir(tree.path())
            .expect_err("a user-controlled system alias must remain rejected");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn pack_shell_header_rejects_wrong_architecture_and_accepts_cross_target_names() {
        let tree = TestTree::new("pack-shell-header");
        let arm_target = "aarch64-apple-darwin";
        let x86_binary = write_pack_shell_fixture(
            &tree.path().join("target"),
            "x86_64-apple-darwin",
            "release",
        );
        let error = read_verified_pack_shell(&x86_binary, arm_target)
            .expect_err("wrong architecture must be rejected");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(
            pack_shell_binary_name("x86_64-pc-windows-msvc"),
            "pack-shell.exe"
        );
        assert_eq!(pack_shell_binary_name(arm_target), "pack-shell");
    }

    #[test]
    fn production_pack_shell_requires_verified_prebuilt_digest() {
        let tree = TestTree::new("pack-shell-prebuilt-digest");
        let binary = tree.path().join("pack-shell");
        let payload = b"prebuilt pack-shell";
        fs::write(&binary, payload).expect("fixture binary should be writable");

        let missing = verify_prebuilt_pack_shell_digest(&binary, payload)
            .expect_err("missing digest must fail closed");
        assert_eq!(missing.kind(), io::ErrorKind::NotFound);

        let mut digest_name = binary.as_os_str().to_os_string();
        digest_name.push(".sha256");
        let digest_path = PathBuf::from(digest_name);
        fs::write(&digest_path, format!("{}\n", "0".repeat(64)))
            .expect("digest should be writable");
        let mismatch = verify_prebuilt_pack_shell_digest(&binary, payload)
            .expect_err("unverified payload must fail closed");
        assert_eq!(mismatch.kind(), io::ErrorKind::InvalidData);

        fs::write(&digest_path, format!("{:x}\n", Sha256::digest(payload)))
            .expect("verified digest should be writable");
        verify_prebuilt_pack_shell_digest(&binary, payload)
            .expect("matching prebuilt digest should pass");

        fs::write(&digest_path, format!("{:X}\n", Sha256::digest(payload)))
            .expect("non-canonical digest should be writable");
        let noncanonical = verify_prebuilt_pack_shell_digest(&binary, payload)
            .expect_err("uppercase digest encoding must fail closed");
        assert_eq!(noncanonical.kind(), io::ErrorKind::InvalidData);

        fs::write(&digest_path, format!("{:x}\n", Sha256::digest(b"tampered")))
            .expect("stale digest should be writable");
        let tampered = verify_prebuilt_pack_shell_digest(&binary, payload)
            .expect_err("stale digest must fail closed");
        assert_eq!(tampered.kind(), io::ErrorKind::InvalidData);
    }

    #[cfg(unix)]
    #[test]
    fn production_pack_shell_rejects_symlinked_digest() {
        let tree = TestTree::new("pack-shell-digest-symlink");
        let binary = tree.path().join("pack-shell");
        let payload = b"prebuilt pack-shell";
        fs::write(&binary, payload).expect("fixture binary should be writable");
        let outside = tree.path().join("outside.sha256");
        fs::write(&outside, format!("{:x}\n", Sha256::digest(payload)))
            .expect("outside digest should be writable");
        let digest_path = tree.path().join("pack-shell.sha256");
        std::os::unix::fs::symlink(&outside, &digest_path)
            .expect("digest symlink should be creatable");

        let error = verify_prebuilt_pack_shell_digest(&binary, payload)
            .expect_err("symlinked digest must fail closed");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn production_pack_shell_never_builds_missing_source_artifact() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("pack-shell-no-source-build");
        let _target = EnvironmentGuard::set_value("TARGET", "aarch64-apple-darwin");
        let _profile = EnvironmentGuard::set_value("PROFILE", "release");
        fs::create_dir_all(tree.path().join("pack-shell"))
            .expect("source directory should be creatable");
        fs::write(
            tree.path().join("pack-shell").join("Cargo.toml"),
            "[package]",
        )
        .expect("source manifest should be writable");

        let error = ensure_pack_shell_binary(tree.path())
            .expect_err("production source-build fallback must remain disabled");
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
        assert!(error.to_string().contains("may not build source"));
    }

    #[test]
    fn isolated_panel_build_overlays_tracked_bundle_without_mutating_source() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("isolated-panel-build");
        let project_dir = tree.path().join("tobkiri_launcher/src-tauri");
        let runtime_root = tree.path().join(APP_SOURCE_DIR);
        let tracked_panel = runtime_root.join(PANEL_RESOURCE_DIR);
        let isolated_panel = tree.path().join("runner-temp/tobkiri-panel-build");
        let staged_root = tree.path().join("staged");
        fs::create_dir_all(&tracked_panel).expect("tracked panel should be creatable");
        fs::create_dir_all(&isolated_panel).expect("isolated panel should be creatable");
        fs::write(tracked_panel.join("index.html"), b"checked-in\n")
            .expect("tracked panel should be writable");
        fs::write(isolated_panel.join("index.html"), b"regenerated\n")
            .expect("isolated panel should be writable");
        let _panel_dir = EnvironmentGuard::set_path(PANEL_BUILD_DIR_ENV, &isolated_panel);

        copy_generated_resource_dirs(&project_dir, &runtime_root, &staged_root, None)
            .expect("isolated panel should be staged");

        assert_eq!(
            fs::read_to_string(runtime_root.join(PANEL_RESOURCE_DIR).join("index.html"))
                .expect("tracked panel should remain readable"),
            "checked-in\n"
        );
        assert_eq!(
            fs::read_to_string(staged_root.join(PANEL_RESOURCE_DIR).join("index.html"))
                .expect("staged panel should be readable"),
            "regenerated\n"
        );
    }

    #[test]
    fn configured_panel_build_must_exist_instead_of_falling_back_to_tracked_output() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("missing-isolated-panel-build");
        let project_dir = tree.path().join("tobkiri_launcher/src-tauri");
        let runtime_root = tree.path().join(APP_SOURCE_DIR);
        let staged_root = tree.path().join("staged");
        fs::create_dir_all(runtime_root.join(PANEL_RESOURCE_DIR))
            .expect("tracked panel should be creatable");
        let missing_panel = tree.path().join("runner-temp/missing-panel");
        let _panel_dir = EnvironmentGuard::set_path(PANEL_BUILD_DIR_ENV, &missing_panel);

        let error = copy_generated_resource_dirs(&project_dir, &runtime_root, &staged_root, None)
            .expect_err("missing configured panel must fail closed");
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
    }

    #[test]
    fn final_package_uses_core_producer_without_external_release_root() {
        let source = include_str!("build.rs");
        let stage = source
            .split("fn stage_presentation_release(")
            .nth(1)
            .expect("stage function should exist")
            .split("fn write_canonical_json")
            .next()
            .expect("stage function should terminate");
        assert!(stage.contains("produce_and_stage_core_presentation_release"));
        assert!(!stage.contains("production package requires"));
    }

    fn release_fixture(tree: &TestTree) -> (PathBuf, PathBuf, PathBuf) {
        let release_root = tree.path().join("release");
        let artifact_id = "shell.tauri.default.linux-x86_64";
        let artifact_path = Path::new("bundled")
            .join("presentation-artifacts")
            .join(artifact_id)
            .join("verified-shell");
        let artifacts = release_root.join(&artifact_path);
        let staged_root = tree.path().join("staged");
        let catalog_path = release_root.join(PRESENTATION_CATALOG_FILENAME);
        fs::create_dir_all(artifacts.parent().expect("artifact should have a parent"))
            .expect("release artifacts should be creatable");
        fs::create_dir_all(staged_root.join("bundled")).expect("staged bundle should be creatable");
        let artifact_payload = b"verified shell artifact";
        fs::write(&artifacts, artifact_payload).expect("release artifact should be writable");
        let (artifact_digest, artifact_size) =
            release_artifact_digest(&artifacts).expect("fixture artifact should hash");
        let entrypoint_digest = byte_digest(artifact_payload);
        let source_identity = "test:source";
        let source_revision = "a".repeat(40);
        let default_profile_path =
            release_root.join("ecosystem/defaultspack/v4/defaults.profile.v4.json");
        let defaultspack_lock_path =
            release_root.join("ecosystem/defaultspack/v4/bundle.lock.json");
        fs::create_dir_all(default_profile_path.parent().expect("Profile has a parent"))
            .expect("Defaults fixture should be creatable");
        fs::write(&default_profile_path, b"{\"profile_id\":\"defaults\"}\n")
            .expect("Profile fixture should be writable");
        let pack_path = defaultspack_lock_path
            .parent()
            .expect("lock has a parent")
            .join("packs/defaults-basepack.pack.v4.json");
        fs::create_dir_all(pack_path.parent().expect("Pack has a parent"))
            .expect("Pack fixture directory should exist");
        fs::write(
            &pack_path,
            include_bytes!(
                "../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json"
            ),
        )
        .expect("real Pack fixture should be writable");
        let pack_digest = byte_digest(&fs::read(&pack_path).expect("Pack should be readable"));
        fs::write(
            &defaultspack_lock_path,
            serde_json::to_vec(&serde_json::json!({
                "entries": [{
                    "path": "packs/defaults-basepack.pack.v4.json",
                    "kind": "pack",
                    "digest": pack_digest,
                }]
            }))
            .expect("lock should encode"),
        )
        .expect("Defaults lock fixture should be writable");
        let default_profile_sha256 =
            byte_digest(&fs::read(&default_profile_path).expect("Profile should exist"));
        let defaultspack_lock_sha256 =
            byte_digest(&fs::read(&defaultspack_lock_path).expect("Defaults lock should exist"));
        let index = serde_json::json!({
            "schema": PRESENTATION_INDEX_SCHEMA,
            "artifact_id": artifact_id,
            "path": artifact_path.to_string_lossy().replace('\\', "/"),
            "sha256": artifact_digest,
            "entrypoint_sha256": entrypoint_digest,
            "size": artifact_size,
            "platform": "linux",
            "architecture": "x86_64",
            "source_identity": source_identity,
            "source_revision": source_revision,
        });
        let index_digest =
            canonical_value_digest(&index, "fixture artifact index").expect("index should hash");
        let mut catalog = serde_json::json!({
            "schema": PRESENTATION_CATALOG_SCHEMA,
            "default_profile_digest": default_profile_sha256,
            "source_manifest_digests": { "defaults-basepack": pack_digest },
            "default_selection": {
                "base_pack_id": "fixture-base",
                "shell_provider_id": "shell.tauri.default",
            },
            "shell_providers": [{
                "provider_id": "shell.tauri.default",
                "artifact_variants": [{
                    "artifact_id": artifact_id,
                    "platform": "linux",
                    "architecture": "x86_64",
                    "path": artifact_path.to_string_lossy().replace('\\', "/"),
                    "sha256": artifact_digest,
                    "entrypoint_sha256": entrypoint_digest,
                    "artifact_ref": "verified-shell",
                    "entrypoint": "verified-shell",
                    "bundle_identifier": "io.tobkiri.shell.tauri",
                    "size": artifact_size,
                    "source_identity": source_identity,
                    "source_revision": source_revision,
                    "production": true,
                    "prebuilt": true,
                    "development_command": serde_json::Value::Null,
                }],
            }],
        });
        let catalog_revision =
            canonical_value_digest(&catalog, "fixture catalog").expect("catalog should hash");
        let lock_body = serde_json::json!({
            "schema": PRESENTATION_LOCK_SCHEMA,
            "catalog_revision": catalog_revision,
            "artifact_index_sha256": index_digest,
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_digest,
            "entrypoint_sha256": entrypoint_digest,
            "platform": "linux",
            "architecture": "x86_64",
            "source_identity": source_identity,
            "source_revision": source_revision,
        });
        let lock_revision =
            canonical_value_digest(&lock_body, "fixture lock").expect("lock should hash");
        let mut lock = lock_body;
        lock["lock_revision"] = serde_json::Value::String(lock_revision);
        catalog["release_binding"] = serde_json::json!({
            "schema": PRESENTATION_RELEASE_SCHEMA,
            "artifact_index_path": "bundled/shell_artifact_index.v4.json",
            "artifact_index_sha256": index_digest,
            "profile_lock_path": "bundled/shell_profile_lock.v4.json",
            "profile_lock_sha256": canonical_value_digest(&lock, "fixture lock").expect("lock should hash"),
            "catalog_revision": catalog_revision,
            "artifact_id": artifact_id,
            "source_identity": source_identity,
            "source_revision": source_revision,
            "platform": "linux",
            "architecture": "x86_64",
        });
        fn write_json(path: &Path, value: &serde_json::Value) {
            fs::write(
                path,
                [
                    serde_json::to_vec_pretty(value).expect("fixture JSON should serialize"),
                    b"\n".to_vec(),
                ]
                .concat(),
            )
            .expect("fixture JSON should be writable");
        }
        write_json(&catalog_path, &catalog);
        write_json(
            &release_root
                .join("bundled")
                .join(PRESENTATION_INDEX_FILENAME),
            &index,
        );
        write_json(
            &release_root
                .join("bundled")
                .join(PRESENTATION_LOCK_FILENAME),
            &lock,
        );
        let catalog_file_digest =
            byte_digest(&fs::read(&catalog_path).expect("catalog should exist"));
        let index_file_digest = byte_digest(
            &fs::read(
                release_root
                    .join("bundled")
                    .join(PRESENTATION_INDEX_FILENAME),
            )
            .expect("index should exist"),
        );
        let lock_file_digest = byte_digest(
            &fs::read(
                release_root
                    .join("bundled")
                    .join(PRESENTATION_LOCK_FILENAME),
            )
            .expect("lock should exist"),
        );
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[7_u8; 32]);
        let public_key = BASE64.encode(signing_key.verifying_key().to_bytes());
        let key_id = "fixture-key";
        let message = [
            PRESENTATION_RELEASE_SCHEMA,
            catalog_file_digest.as_str(),
            index_file_digest.as_str(),
            lock_file_digest.as_str(),
            default_profile_sha256.as_str(),
            defaultspack_lock_sha256.as_str(),
            source_identity,
            source_revision.as_str(),
            "linux",
            "x86_64",
            artifact_id,
            key_id,
        ]
        .join("\0");
        let signature = BASE64.encode(signing_key.sign(message.as_bytes()).to_bytes());
        let release = serde_json::json!({
            "schema": PRESENTATION_RELEASE_SCHEMA,
            "catalog_path": "bundled/presentation_catalog.json",
            "catalog_sha256": catalog_file_digest,
            "artifact_index_path": "bundled/shell_artifact_index.v4.json",
            "artifact_index_sha256": index_file_digest,
            "profile_lock_path": "bundled/shell_profile_lock.v4.json",
            "profile_lock_sha256": lock_file_digest,
            "default_profile_path": "ecosystem/defaultspack/v4/defaults.profile.v4.json",
            "default_profile_sha256": default_profile_sha256,
            "defaultspack_lock_path": "ecosystem/defaultspack/v4/bundle.lock.json",
            "defaultspack_lock_sha256": defaultspack_lock_sha256,
            "artifact_id": artifact_id,
            "platform": "linux",
            "architecture": "x86_64",
            "source_identity": source_identity,
            "source_revision": source_revision,
            "key_id": key_id,
            "public_key": public_key,
            "signature": signature,
        });
        write_json(
            &release_root
                .join("bundled")
                .join(PRESENTATION_RELEASE_FILENAME),
            &release,
        );
        (release_root, staged_root, catalog_path)
    }

    fn host_fixture(tree: &TestTree) -> (PathBuf, PathBuf) {
        let source_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("Launcher should live under the repository")
            .join(APP_SOURCE_DIR);
        let staged_root = tree.path().join("staged-host");
        let host_root = staged_root.join("tobkiri_host");
        fs::create_dir_all(&host_root).expect("Host package should be creatable");
        for filename in
            canonical_host_files(&source_root).expect("Host inventory should be readable")
        {
            fs::copy(
                source_root.join("tobkiri_host").join(&filename),
                host_root.join(filename),
            )
            .expect("Host resource should be copied exactly");
        }
        (staged_root, source_root)
    }

    #[test]
    fn canonical_host_inventory_is_exact() {
        let tree = TestTree::new("host-inventory");
        let (staged_root, source_root) = host_fixture(&tree);
        verify_canonical_host_package(&staged_root, &source_root)
            .expect("exact Host package should be accepted");

        fs::write(staged_root.join("tobkiri_host/unlisted.py"), b"pass\n")
            .expect("unlisted resource should be writable");
        assert!(verify_canonical_host_package(&staged_root, &source_root).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn canonical_host_inventory_rejects_symlink() {
        let tree = TestTree::new("host-symlink");
        let (staged_root, source_root) = host_fixture(&tree);
        let runtime = staged_root.join("tobkiri_host/runtime.py");
        fs::remove_file(&runtime).expect("runtime fixture should be removable");
        std::os::unix::fs::symlink(tree.path(), &runtime)
            .expect("Host symlink should be creatable");
        assert!(verify_canonical_host_package(&staged_root, &source_root).is_err());
    }

    #[test]
    fn release_stage_then_verify_uses_exact_catalog_file_paths() {
        let _environment_lock = environment_lock();
        let tree = TestTree::new("stage-verify");
        let (release_root, staged_root, catalog) = release_fixture(&tree);

        let _tauri_config = EnvironmentGuard::set_value(
            "TAURI_CONFIG",
            r#"{"identifier":"io.tobkiri.shell.tauri","mainBinaryName":"tobkiri-shell"}"#,
        );
        let _target = EnvironmentGuard::set_value("TARGET", "x86_64-unknown-linux-gnu");
        let _release_root =
            EnvironmentGuard::set_path(PRESENTATION_RELEASE_ROOT_ENV, &release_root);
        let source_catalog = stage_presentation_release(&staged_root)
            .expect("release should stage")
            .expect("release staging should return a catalog");
        let staged_catalog = staged_root
            .join("bundled")
            .join(PRESENTATION_CATALOG_FILENAME);

        assert_ne!(source_catalog, catalog);
        assert!(source_catalog.starts_with(&staged_root));
        assert!(source_catalog.is_file());
        assert!(staged_catalog.is_file());
        verify_staged_catalog(&source_catalog, &staged_catalog)
            .expect("staged catalog should match the release catalog");
        assert!(staged_root
            .join("bundled")
            .join("presentation-artifacts")
            .join("shell.tauri.default.linux-x86_64")
            .join("verified-shell")
            .is_file());
    }

    #[test]
    fn snapshot_rejects_mutation_of_every_signed_identity_during_copy() {
        for relative in [
            "presentation_catalog.json",
            "bundled/shell_artifact_index.v4.json",
            "bundled/shell_profile_lock.v4.json",
            "bundled/presentation_release.v4.json",
            "ecosystem/defaultspack/v4/defaults.profile.v4.json",
            "ecosystem/defaultspack/v4/bundle.lock.json",
        ] {
            let tree = TestTree::new(&format!("snapshot-race-{}", relative.replace('/', "-")));
            let (release_root, _, _) = release_fixture(&tree);
            let snapshot = tree.path().join("private-snapshot");
            let target = release_root.join(relative);
            let error = snapshot_presentation_release_with_hook(&release_root, &snapshot, || {
                fs::write(&target, b"mutated during snapshot")
                    .expect("race mutation should be writable");
            })
            .expect_err("source mutation during snapshot must fail closed");
            assert!(error.to_string().contains("mutated or copied partially"));
        }
    }

    #[test]
    fn snapshot_rejects_missing_extra_and_partial_release_trees() {
        let tree = TestTree::new("snapshot-tree-shape");
        let (release_root, _, _) = release_fixture(&tree);
        fs::write(release_root.join("unexpected.json"), b"extra")
            .expect("extra fixture should be writable");
        let error =
            snapshot_presentation_release(&release_root, &tree.path().join("extra-snapshot"))
                .expect_err("extra release entry must fail closed");
        assert!(error.to_string().contains("extra entry"));

        fs::remove_file(release_root.join("unexpected.json"))
            .expect("extra fixture should be removable");
        fs::remove_file(
            release_root
                .join("bundled")
                .join(PRESENTATION_INDEX_FILENAME),
        )
        .expect("required fixture should be removable");
        let error =
            snapshot_presentation_release(&release_root, &tree.path().join("missing-snapshot"))
                .expect_err("missing release entry must fail closed");
        assert!(error.to_string().contains("missing required file"));
    }

    #[test]
    fn verification_rejects_an_extra_artifact_sibling() {
        let tree = TestTree::new("extra-artifact-sibling");
        let (release_root, _, _) = release_fixture(&tree);
        let rogue_artifact = release_root
            .join("bundled/presentation-artifacts")
            .join("shell.tauri.default.linux-x86_64-stale")
            .join("verified-shell");
        fs::create_dir_all(
            rogue_artifact
                .parent()
                .expect("rogue artifact should have a parent"),
        )
        .expect("rogue artifact directory should be creatable");
        fs::write(&rogue_artifact, b"stale artifact").expect("rogue artifact should be writable");

        let error = verify_presentation_release(&release_root)
            .expect_err("an unsigned artifact sibling must fail closed");
        assert!(error.to_string().contains("extra artifact entry"));
    }

    #[test]
    fn verification_rejects_stale_missing_extra_and_wrong_pack_bindings() {
        for mutation in ["stale", "missing", "extra", "wrong"] {
            let tree = TestTree::new(&format!("pack-binding-{mutation}"));
            let (release_root, _, catalog_path) = release_fixture(&tree);
            let mut catalog: serde_json::Value = serde_json::from_slice(
                &fs::read(&catalog_path).expect("catalog fixture should be readable"),
            )
            .expect("catalog fixture should parse");
            let bindings = catalog["source_manifest_digests"]
                .as_object_mut()
                .expect("fixture bindings should be an object");
            match mutation {
                "stale" | "wrong" => {
                    bindings.insert(
                        "defaults-basepack".into(),
                        serde_json::Value::String(format!("sha256:{}", "0".repeat(64))),
                    );
                }
                "missing" => {
                    bindings.remove("defaults-basepack");
                }
                "extra" => {
                    bindings.insert(
                        "unselected".into(),
                        serde_json::Value::String(format!("sha256:{}", "1".repeat(64))),
                    );
                }
                _ => unreachable!(),
            }
            write_canonical_json(&catalog_path, &catalog).expect("mutated catalog should write");
            let error = verify_catalog_source_manifest_digests(
                &catalog,
                &release_root.join("ecosystem/defaultspack/v4/bundle.lock.json"),
            )
            .expect_err("Pack binding mismatch must fail closed");
            let expected = match mutation {
                "stale" | "wrong" => {
                    "catalog selected Pack set differs from the exact Defaults lock Pack entries"
                }
                "missing" => "catalog selected Pack set is empty",
                "extra" => "Defaults lock is missing a selected catalog Pack",
                _ => unreachable!(),
            };
            assert!(error.to_string().contains(expected));
        }
    }

    #[test]
    fn lock_and_catalog_cannot_share_a_forged_pack_digest() {
        let tree = TestTree::new("pack-binding-shared-forgery");
        let (release_root, _, catalog_path) = release_fixture(&tree);
        let lock_path = release_root.join("ecosystem/defaultspack/v4/bundle.lock.json");
        let forged = format!("sha256:{}", "a".repeat(64));
        let mut lock: serde_json::Value =
            serde_json::from_slice(&fs::read(&lock_path).expect("lock fixture should be readable"))
                .expect("lock fixture should parse");
        lock["entries"][0]["digest"] = serde_json::Value::String(forged.clone());
        fs::write(
            &lock_path,
            serde_json::to_vec(&lock).expect("lock should encode"),
        )
        .expect("forged lock should write");
        let mut catalog: serde_json::Value = serde_json::from_slice(
            &fs::read(&catalog_path).expect("catalog fixture should be readable"),
        )
        .expect("catalog fixture should parse");
        catalog["source_manifest_digests"]["defaults-basepack"] = serde_json::Value::String(forged);
        let error = verify_catalog_source_manifest_digests(&catalog, &lock_path)
            .expect_err("shared forged digest must not authenticate Pack bytes");
        assert!(error.to_string().contains("exact Pack bytes"));
    }

    #[test]
    fn selected_pack_binding_rejects_missing_duplicate_unsafe_path_wrong_id_and_digest() {
        for mutation in ["missing", "duplicate", "path", "id", "digest"] {
            let tree = TestTree::new(&format!("selected-pack-{mutation}"));
            let (release_root, _, catalog_path) = release_fixture(&tree);
            let lock_path = release_root.join("ecosystem/defaultspack/v4/bundle.lock.json");
            let mut lock: serde_json::Value =
                serde_json::from_slice(&fs::read(&lock_path).expect("lock fixture should read"))
                    .expect("lock fixture should parse");
            match mutation {
                "missing" => lock["entries"] = serde_json::json!([]),
                "duplicate" => {
                    let duplicate = lock["entries"][0].clone();
                    lock["entries"].as_array_mut().unwrap().push(duplicate);
                }
                "path" => {
                    lock["entries"][0]["path"] =
                        serde_json::Value::String("packs/../wrong.pack.v4.json".into());
                }
                "id" => {
                    let pack_path = release_root
                        .join("ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json");
                    let mut pack: serde_json::Value = serde_json::from_slice(
                        &fs::read(&pack_path).expect("Pack fixture should read"),
                    )
                    .expect("Pack fixture should parse");
                    pack["pack"]["id"] = serde_json::Value::String("wrong".into());
                    let bytes = serde_json::to_vec(&pack).expect("Pack should encode");
                    fs::write(&pack_path, &bytes).expect("Pack mutation should write");
                    lock["entries"][0]["digest"] = serde_json::Value::String(byte_digest(&bytes));
                }
                "digest" => {
                    lock["entries"][0]["digest"] =
                        serde_json::Value::String(format!("sha256:{}", "b".repeat(64)));
                }
                _ => unreachable!(),
            }
            fs::write(
                &lock_path,
                serde_json::to_vec(&lock).expect("lock should encode"),
            )
            .expect("lock mutation should write");
            let catalog: serde_json::Value = serde_json::from_slice(
                &fs::read(&catalog_path).expect("catalog fixture should read"),
            )
            .expect("catalog fixture should parse");
            verify_catalog_source_manifest_digests(&catalog, &lock_path)
                .expect_err("selected Pack mismatch must fail closed");
        }
    }

    #[test]
    fn nonselected_lock_pack_is_allowed_but_not_added_to_catalog() {
        let tree = TestTree::new("nonselected-pack-extra");
        let (release_root, _, catalog_path) = release_fixture(&tree);
        let lock_path = release_root.join("ecosystem/defaultspack/v4/bundle.lock.json");
        let bundle_root = lock_path.parent().expect("lock has bundle root");
        let extra_path = bundle_root.join("packs/shell.cli.default.pack.v4.json");
        fs::write(
            &extra_path,
            include_bytes!(
                "../../tobkiri_runtime/ecosystem/defaultspack/v4/packs/shell.cli.default.pack.v4.json"
            ),
        )
        .expect("extra Pack fixture should copy");
        let mut lock: serde_json::Value =
            serde_json::from_slice(&fs::read(&lock_path).expect("lock fixture should read"))
                .expect("lock fixture should parse");
        lock["entries"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "path": "packs/shell.cli.default.pack.v4.json",
                "kind": "pack",
                "digest": byte_digest(&fs::read(&extra_path).expect("extra Pack should read")),
            }));
        fs::write(
            &lock_path,
            serde_json::to_vec(&lock).expect("lock should encode"),
        )
        .expect("lock should write");
        let catalog: serde_json::Value =
            serde_json::from_slice(&fs::read(&catalog_path).expect("catalog should read"))
                .expect("catalog should parse");
        let selected = catalog["source_manifest_digests"].as_object().unwrap();
        let updated = selected_source_manifest_digests_from_lock(&lock_path, selected)
            .expect("nonselected Pack extra should be allowed");
        assert_eq!(updated.len(), selected.len());
        assert!(!updated.contains_key("shell.cli.default"));
    }

    #[test]
    fn real_lock_preserves_catalog_entries_and_aliases() {
        let tree = TestTree::new("real-lock-catalog-binding");
        let bundle = tree.path().join("bundle");
        fs::create_dir_all(&bundle).expect("bundle fixture should exist");
        let lock_path = bundle.join("bundle.lock.json");
        fs::write(
            &lock_path,
            include_bytes!("../../tobkiri_runtime/ecosystem/defaultspack/v4/bundle.lock.json"),
        )
        .expect("real lock fixture should write");
        let lock: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../../tobkiri_runtime/ecosystem/defaultspack/v4/bundle.lock.json"
        ))
        .expect("real lock should parse");
        for entry in lock["entries"].as_array().expect("real lock entries") {
            if entry["kind"] != "pack" {
                continue;
            }
            let relative = entry["path"].as_str().expect("real Pack path");
            let destination = bundle.join(relative);
            fs::create_dir_all(destination.parent().expect("Pack has parent"))
                .expect("Pack parent should exist");
            fs::copy(
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../../tobkiri_runtime/ecosystem/defaultspack/v4")
                    .join(relative),
                destination,
            )
            .expect("real Pack fixture should copy");
        }
        let catalog: serde_json::Value =
            serde_json::from_slice(include_bytes!("bundled/presentation_catalog.json"))
                .expect("canonical catalog should parse");
        let selected = catalog["source_manifest_digests"]
            .as_object()
            .expect("canonical selection should exist");
        assert_eq!(selected.len(), 24);
        let updated = selected_source_manifest_digests_from_lock(&lock_path, selected)
            .expect("real lock aliases must bind by nested pack.id");
        assert_eq!(updated.len(), selected.len());
        for alias in [
            "rumi_file_inspect_pack",
            "rumi_host_authority_bridge_pack",
            "rumi_workspace_mount_pack",
            "tobkiri_host_pack_control",
        ] {
            assert!(updated.contains_key(alias));
        }
        assert!(!updated.contains_key("shell.cli.default"));
        assert!(!updated.contains_key("dev.tauri.toolchain.default"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn core_transaction_cleanup_refuses_name_swap() {
        let tree = TestTree::new("core-transaction-name-swap");
        let mut guard =
            CoreTransactionGuard::create(tree.path()).expect("transaction should be created");
        fs::write(guard.path().join("owned"), b"owned").expect("owned file should write");
        guard.seal_inventory().expect("inventory should seal");
        let original = guard.path().with_extension("original");
        fs::rename(guard.path(), &original).expect("transaction should move");
        fs::create_dir(guard.path()).expect("replacement should be created");
        fs::write(guard.path().join("victim"), b"victim").expect("victim should write");
        let error = guard
            .cleanup()
            .expect_err("replacement cleanup must fail closed");
        assert!(error.to_string().contains("replaced"));
        assert!(guard_path_victim_exists(tree.path()));

        fn guard_path_victim_exists(parent: &Path) -> bool {
            fs::read_dir(parent)
                .expect("parent should read")
                .filter_map(Result::ok)
                .any(|entry| entry.path().join("victim").is_file())
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn core_transaction_without_complete_inventory_leaves_residue() {
        let tree = TestTree::new("core-transaction-incomplete");
        let guard =
            CoreTransactionGuard::create(tree.path()).expect("transaction should be created");
        let path = guard.path().to_owned();
        let error = guard
            .cleanup()
            .expect_err("unknown ownership must retain residue");
        assert!(error.to_string().contains("incomplete"));
        assert!(path.is_dir());
    }

    #[cfg(target_os = "macos")]
    fn write_read_only_staged_runtime_fixture(root: &Path) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;

        let nested = root.join("bundle");
        fs::create_dir_all(&nested).expect("staged fixture should be creatable");
        fs::write(nested.join("entry.txt"), b"host-sealed bytes")
            .expect("staged fixture file should be writable");
        write_runtime_resource_manifest(root).expect("staged fixture should seal");
        fs::set_permissions(&nested, fs::Permissions::from_mode(0o555))
            .expect("nested fixture should be sealed");
        fs::set_permissions(root, fs::Permissions::from_mode(0o555))
            .expect("staged fixture root should be sealed");
        nested
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn staged_xattr_transport_reproduces_read_only_failure_and_binds_delta() {
        use std::os::unix::fs::PermissionsExt;

        let tree = TestTree::new("staged-xattr-cleanup");
        let staged = tree.path().join("gen/app");
        fs::create_dir_all(&staged).expect("staged root should be creatable");
        let resource = staged.join("sealed.txt");
        fs::write(&resource, b"sealed resource bytes").expect("fixture should be writable");
        let wrote = Command::new(MACOS_XATTR_PATH)
            .args(["-w", "io.tobkiri.test", "present"])
            .arg(&resource)
            .status()
            .expect("canonical xattr should run");
        assert!(wrote.success());
        fs::set_permissions(&resource, fs::Permissions::from_mode(0o444))
            .expect("fixture should become read-only");

        let reproduced = Command::new(MACOS_XATTR_PATH)
            .args(["-c", "-r"])
            .arg(&staged)
            .status()
            .expect("canonical xattr should run");
        assert!(
            !reproduced.success(),
            "recursive xattr cleanup must reproduce the read-only failure"
        );

        prepare_staged_macos_xattr_transport(&staged)
            .expect("Host-owned transport view should admit canonical xattr");
        assert_eq!(fs::read(&resource).unwrap(), b"sealed resource bytes");
        assert_eq!(
            fs::metadata(&resource).unwrap().permissions().mode() & 0o777,
            0o644
        );
        let admitted = Command::new(MACOS_XATTR_PATH)
            .args(["-c", "-r"])
            .arg(&staged)
            .status()
            .expect("canonical xattr should run on the transport view");
        assert!(admitted.success());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn tauri_resource_copy_reset_accepts_target_qualified_profile() {
        let tree = TestTree::new("tauri-resource-copy-target-qualified");
        let target_root = tree.path().join("target");
        let profile_root = target_root.join("aarch64-apple-darwin/release");
        let out = profile_root.join("build/tobkiri-launcher-fixture/out");
        fs::create_dir_all(&out).expect("Cargo OUT_DIR fixture should be creatable");
        let resource = profile_root.join("app");
        let nested = write_read_only_staged_runtime_fixture(&resource);
        let host_sentinel = target_root.join("release/app/untouched");
        fs::create_dir_all(host_sentinel.parent().unwrap()).unwrap();
        fs::write(&host_sentinel, b"host profile").unwrap();

        let reset = reset_tauri_resource_copy_for_cargo_at(
            &out,
            &target_root,
            "aarch64-apple-darwin",
            "release",
        )
        .expect("target-qualified Tauri resource cache should reset");

        assert_eq!(reset, resource);
        assert!(resource.is_dir());
        assert!(!nested.exists());
        assert!(fs::read_dir(&resource).unwrap().next().is_none());
        assert_eq!(fs::read(&host_sentinel).unwrap(), b"host profile");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn tauri_resource_copy_reset_accepts_implicit_host_profile() {
        let tree = TestTree::new("tauri-resource-copy-implicit-host");
        let target_root = tree.path().join("target");
        let profile_root = target_root.join("debug");
        let out = profile_root.join("build/tobkiri-launcher-fixture/out");
        fs::create_dir_all(&out).expect("Cargo OUT_DIR fixture should be creatable");
        let resource = profile_root.join("app");
        let nested = write_read_only_staged_runtime_fixture(&resource);
        let target_sentinel = target_root.join("aarch64-apple-darwin/debug/app/untouched");
        fs::create_dir_all(target_sentinel.parent().unwrap()).unwrap();
        fs::write(&target_sentinel, b"target profile").unwrap();

        let reset = reset_tauri_resource_copy_for_cargo_at(
            &out,
            &target_root,
            "aarch64-apple-darwin",
            "debug",
        )
        .expect("implicit-host Tauri resource cache should reset");

        assert_eq!(reset, resource);
        assert!(resource.is_dir());
        assert!(!nested.exists());
        assert!(fs::read_dir(&resource).unwrap().next().is_none());
        assert_eq!(fs::read(&target_sentinel).unwrap(), b"target profile");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn tauri_resource_copy_reset_rejects_unbound_or_malformed_out_dir() {
        use std::os::unix::fs::PermissionsExt;

        let tree = TestTree::new("tauri-resource-copy-rejected-layouts");
        let target_root = tree.path().join("target");
        let resource = target_root.join("debug/app");
        let nested = write_read_only_staged_runtime_fixture(&resource);
        let rejected = [
            tree.path()
                .join("outside/debug/build/tobkiri-launcher-fixture/out"),
            target_root.join("x86_64-apple-darwin/debug/build/tobkiri-launcher-fixture/out"),
            target_root.join("debug/build/out"),
            target_root.join("debug/build/tobkiri-launcher-fixture/not-out"),
            target_root.join("debug/build/tobkiri-launcher-fixture/extra/out"),
        ];

        for out in rejected {
            fs::create_dir_all(&out).expect("rejected OUT_DIR fixture should be creatable");
            reset_tauri_resource_copy_for_cargo_at(
                &out,
                &target_root,
                "aarch64-apple-darwin",
                "debug",
            )
            .expect_err("unbound or malformed Cargo OUT_DIR must be rejected");
            assert!(
                nested.exists(),
                "rejected OUT_DIR mutated the resource cache"
            );
        }

        let valid_out = target_root.join("debug/build/tobkiri-launcher-fixture/out");
        fs::create_dir_all(&valid_out).expect("valid OUT_DIR fixture should be creatable");
        for invalid_profile in ["", ".", "..", "debug/escape", r"debug\escape"] {
            reset_tauri_resource_copy_for_cargo_at(
                &valid_out,
                &target_root,
                "aarch64-apple-darwin",
                invalid_profile,
            )
            .expect_err("invalid Cargo PROFILE must be rejected");
            assert!(
                nested.exists(),
                "invalid PROFILE mutated the resource cache"
            );
        }

        fs::set_permissions(&nested, fs::Permissions::from_mode(0o755))
            .expect("nested fixture should be restored for cleanup");
        fs::set_permissions(&resource, fs::Permissions::from_mode(0o755))
            .expect("resource fixture should be restored for cleanup");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn staged_runtime_reset_creates_a_missing_host_owned_root() {
        use std::os::unix::fs::PermissionsExt;

        let tree = TestTree::new("staged-runtime-new-root");
        let staged = tree.path().join("gen/app");

        reset_staged_runtime(&staged)
            .expect("an absent generated runtime root should be created safely");

        assert!(staged.is_dir());
        assert_eq!(
            fs::metadata(&staged)
                .expect("new staged root should have metadata")
                .permissions()
                .mode()
                & 0o777,
            0o755
        );
        assert!(
            fs::read_dir(&staged)
                .expect("new staged root should be readable")
                .next()
                .is_none(),
            "new staging must have no inherited content"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn staged_runtime_reset_unseals_only_the_host_sealed_tree() {
        use std::os::unix::fs::PermissionsExt;

        let tree = TestTree::new("staged-runtime-reset");
        let staged = tree.path().join("gen/app");
        fs::create_dir_all(&staged).expect("staged root should be creatable");
        let nested = write_read_only_staged_runtime_fixture(&staged);

        reset_staged_runtime(&staged).expect("host-sealed staging should reset");
        assert!(staged.is_dir());
        assert!(!nested.exists());
        assert!(!staged.join(RUNTIME_RESOURCE_MANIFEST).exists());
        assert_eq!(
            fs::metadata(&staged)
                .expect("new staged root should exist")
                .permissions()
                .mode()
                & 0o777,
            0o755
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn staged_runtime_reset_reaps_an_empty_partial_root() {
        use std::os::unix::fs::PermissionsExt;

        let tree = TestTree::new("staged-runtime-empty-partial");
        let staged = tree.path().join("gen/app");
        fs::create_dir_all(&staged).expect("staged root should be creatable");

        reset_staged_runtime(&staged).expect("empty partial staging should reset");

        assert!(staged.is_dir());
        assert!(!staged.join(RUNTIME_RESOURCE_MANIFEST).exists());
        assert_eq!(
            fs::metadata(&staged)
                .expect("new staged root should exist")
                .permissions()
                .mode()
                & 0o777,
            0o755
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn staged_runtime_reset_retains_nonempty_unsealed_residue() {
        let tree = TestTree::new("staged-runtime-unsealed-residue");
        let staged = tree.path().join("gen/app");
        fs::create_dir_all(&staged).expect("staged root should be creatable");
        fs::write(staged.join("partial-entry"), b"partial build")
            .expect("partial residue should be writable");

        let error = reset_staged_runtime(&staged)
            .expect_err("nonempty unsealed staging must remain fail-closed");

        assert!(error.to_string().contains("seal manifest is missing"));
        assert!(staged.join("partial-entry").is_file());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn staged_runtime_reset_rejects_extra_and_replaced_entries() {
        use std::os::unix::fs::PermissionsExt;

        let tree = TestTree::new("staged-runtime-negative");
        let staged = tree.path().join("gen/app");
        fs::create_dir_all(&staged).expect("staged root should be creatable");
        let nested = write_read_only_staged_runtime_fixture(&staged);
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o755))
            .expect("staged fixture should be writable for the negative case");
        fs::write(staged.join("unowned-extra"), b"must remain")
            .expect("extra fixture should be writable before sealing");
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o555))
            .expect("staged fixture root should be resealed");
        let error = reset_staged_runtime(&staged).expect_err("extra entry must fail closed");
        assert!(error.to_string().contains("exact owned tree"));
        assert!(staged.join("unowned-extra").is_file());
        fs::set_permissions(&nested, fs::Permissions::from_mode(0o755))
            .expect("nested fixture should be restored for cleanup");
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o755))
            .expect("staged fixture should be restored for cleanup");

        fs::remove_dir_all(&staged).expect("extra fixture should be removable");
        fs::create_dir_all(&staged).expect("hardlink fixture root should be creatable");
        let nested = write_read_only_staged_runtime_fixture(&staged);
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o755))
            .expect("hardlink fixture root should be writable before mutation");
        let outside = tree.path().join("outside-hardlink");
        fs::write(&outside, b"outside bytes").expect("hardlink source should be writable");
        fs::hard_link(&outside, staged.join("linked-extra"))
            .expect("hardlink fixture should be creatable");
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o555))
            .expect("hardlink fixture root should be resealed");
        let error = reset_staged_runtime(&staged).expect_err("hardlink must fail closed");
        assert!(error.to_string().contains("special or linked"));
        assert!(outside.is_file());
        fs::set_permissions(&nested, fs::Permissions::from_mode(0o755))
            .expect("nested hardlink fixture should be restored for cleanup");
        fs::set_permissions(&staged, fs::Permissions::from_mode(0o755))
            .expect("hardlink fixture should be restored for cleanup");

        let replacement = tree.path().join("replacement");
        fs::create_dir_all(&replacement).expect("replacement should be creatable");
        fs::write(replacement.join("victim"), b"victim")
            .expect("replacement victim should be writable");
        fs::remove_dir_all(&staged).expect("original fixture should be removable");
        std::os::unix::fs::symlink(&replacement, &staged)
            .expect("replacement symlink should be creatable");
        let error = reset_staged_runtime(&staged).expect_err("root symlink must fail closed");
        assert!(error.to_string().contains("symlink"));
        assert!(replacement.join("victim").is_file());
        fs::remove_file(&staged).expect("replacement symlink should be removable");
    }

    #[cfg(any(unix, windows))]
    #[test]
    fn snapshot_rejects_hardlinked_release_files() {
        let tree = TestTree::new("snapshot-hardlink");
        let (release_root, _, catalog) = release_fixture(&tree);
        let outside = tree.path().join("outside-catalog.json");
        fs::rename(&catalog, &outside).expect("catalog should move outside");
        fs::hard_link(&outside, &catalog).expect("hardlink fixture should be creatable");
        let error =
            snapshot_presentation_release(&release_root, &tree.path().join("hardlink-snapshot"))
                .expect_err("hardlinked release file must fail closed");
        assert!(error.to_string().contains("must have one link"));
    }

    #[test]
    fn source_mutation_after_snapshot_cannot_change_staged_bytes() {
        let tree = TestTree::new("snapshot-post-verify-mutation");
        let (release_root, staged_root, catalog) = release_fixture(&tree);
        let snapshot = tree.path().join("private-snapshot");
        snapshot_presentation_release(&release_root, &snapshot)
            .expect("release snapshot should succeed");
        verify_presentation_release(&snapshot).expect("snapshot should verify");
        fs::write(&catalog, b"mutated after snapshot verification")
            .expect("mutable source should be changeable");

        let staged_catalog = stage_presentation_release_from_snapshot(&staged_root, &snapshot)
            .expect("verified snapshot should stage")
            .expect("staged catalog should be returned");
        assert_eq!(
            fs::read(&staged_catalog).expect("staged catalog should be readable"),
            fs::read(snapshot.join(PRESENTATION_CATALOG_FILENAME))
                .expect("snapshot catalog should be readable")
        );
        assert_ne!(
            fs::read(&staged_catalog).expect("staged catalog should remain readable"),
            fs::read(&catalog).expect("mutated source should be readable")
        );
    }

    #[test]
    fn core_staging_replaces_source_only_profile_artifacts() {
        let tree = TestTree::new("core-source-only-profile-artifacts");
        let (release_root, staged_root, _) = release_fixture(&tree);
        let staged_bundle = staged_root.join("ecosystem/defaultspack/v4");
        fs::create_dir_all(&staged_bundle).expect("staged Defaults bundle should be creatable");
        for filename in SOURCE_ONLY_PROFILE_ARTIFACTS {
            fs::write(
                staged_bundle.join(filename),
                b"tracked source-only artifact",
            )
            .expect("tracked source-only artifact should be creatable");
        }
        let retained = staged_bundle.join("unrelated-tracked-file");
        fs::write(&retained, b"retained").expect("unrelated staged file should be creatable");

        stage_core_verified_release(&staged_root, &release_root)
            .expect("Core staging should replace the packaged Defaults closure");

        for filename in SOURCE_ONLY_PROFILE_ARTIFACTS {
            assert!(
                fs::symlink_metadata(staged_bundle.join(filename))
                    .expect_err("source-only Profile artifact must be absent")
                    .kind()
                    == io::ErrorKind::NotFound
            );
        }
        assert_eq!(
            fs::read(&retained).expect("unrelated staged file should remain"),
            b"retained"
        );
    }

    #[cfg(unix)]
    #[test]
    fn source_only_profile_artifact_removal_rejects_symlink_and_directory() {
        use std::os::unix::fs::symlink;

        let tree = TestTree::new("source-only-profile-artifact-types");
        let bundle = tree.path().join("staged/ecosystem/defaultspack/v4");
        fs::create_dir_all(&bundle).expect("staged Defaults bundle should be creatable");
        let outside = tree.path().join("outside-profile-artifact");
        fs::write(&outside, b"outside").expect("outside fixture should be creatable");
        symlink(&outside, bundle.join(SOURCE_ONLY_PROFILE_ARTIFACTS[0]))
            .expect("source-only fixture symlink should be creatable");

        let error = remove_source_only_profile_artifacts(&bundle)
            .expect_err("source-only Profile symlink must fail closed");
        assert!(error.to_string().contains("symlink"));
        assert_eq!(
            fs::read(&outside).expect("outside target should remain"),
            b"outside"
        );

        fs::remove_file(bundle.join(SOURCE_ONLY_PROFILE_ARTIFACTS[0]))
            .expect("fixture symlink should be removable");
        fs::create_dir(bundle.join(SOURCE_ONLY_PROFILE_ARTIFACTS[1]))
            .expect("source-only directory fixture should be creatable");
        let error = remove_source_only_profile_artifacts(&bundle)
            .expect_err("source-only Profile directory must fail closed");
        assert!(error.to_string().contains("absent or regular"));
    }

    #[test]
    fn complete_staged_release_verification_rechecks_every_signed_file() {
        for relative in [
            "bundled/presentation_catalog.json",
            "bundled/shell_artifact_index.v4.json",
            "bundled/shell_profile_lock.v4.json",
            "bundled/presentation_release.v4.json",
            "ecosystem/defaultspack/v4/defaults.profile.v4.json",
            "ecosystem/defaultspack/v4/bundle.lock.json",
        ] {
            let tree = TestTree::new(&format!("staged-recheck-{}", relative.replace('/', "-")));
            let (release_root, _, _) = release_fixture(&tree);
            let staged = tree.path().join("complete-staged");
            copy_release_tree(&release_root, &staged).expect("release should copy to stage");
            let source_catalog = staged.join(PRESENTATION_CATALOG_FILENAME);
            let staged_catalog = staged.join("bundled").join(PRESENTATION_CATALOG_FILENAME);
            fs::rename(&source_catalog, &staged_catalog)
                .expect("catalog should move to packaged location");
            verify_presentation_release_at(&staged, &staged_catalog)
                .expect("complete staged release should verify before tampering");
            fs::write(staged.join(relative), b"tampered staged release")
                .expect("staged tamper should be writable");
            verify_presentation_release_at(&staged, &staged_catalog)
                .expect_err("every staged signed identity must be rechecked");
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn core_packager_output_uses_verified_runtime_and_stages_bundle() {
        let _environment = environment_lock();
        let tree = TestTree::new("core-packager-staging");
        let repository_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("Launcher should live under the repository")
            .canonicalize()
            .expect("repository root should resolve");
        let runtime_root = repository_root.join(APP_SOURCE_DIR);
        let trusted_manifest =
            fs::read(runtime_root.join("packaged_defaultspack_source_manifest.v1.json"))
                .expect("committed source manifest should be readable");
        let bundle_root = tree.path().join("staged/ecosystem/defaultspack/v4");
        copy_dir_recursive(
            &runtime_root.join("ecosystem/defaultspack/v4"),
            &bundle_root,
        )
        .expect("canonical Defaults bundle should stage");
        let source_artifact = tree.path().join("source/Tobkiri.AppImage");
        fs::create_dir_all(source_artifact.parent().expect("artifact has a parent"))
            .expect("artifact fixture root should be creatable");
        let artifact = [
            0x7f, b'E', b'L', b'F', 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x3e, 0,
        ];
        fs::write(&source_artifact, artifact).expect("artifact fixture should be writable");
        let artifact_root = tree
            .path()
            .join("staged/ecosystem/defaultspack/platform-artifacts");
        let source_revision = "0123456789abcdef0123456789abcdef01234567";
        let source_tree = "89abcdef0123456789abcdef0123456789abcdef";
        let snapshot_parent = tree.path().join("snapshots");
        let projection = DefaultsPackagingProjection {
            source_artifact: &source_artifact,
            bundle_root: &bundle_root,
            artifact_root: &artifact_root,
            relative_path: "Tobkiri.AppImage",
            entrypoint: "Tobkiri.AppImage",
            platform: "linux",
            architecture: "x86_64",
            bundle_identity: "io.tobkiri.shell.tauri",
        };
        let output = run_formal_defaults_packaging(
            DefaultsPackagingRequest {
                repository_root: &repository_root,
                snapshot_parent: &snapshot_parent,
                trusted_source_manifest: &trusted_manifest,
                source_revision,
                source_tree,
                projection,
            },
            |output| Ok(output),
        )
        .expect("core packager fixture should produce a verified bundle");
        assert_eq!(
            output.default_profile_sha256,
            byte_digest(&fs::read(bundle_root.join("defaults.profile.v4.json")).unwrap())
        );
        assert_eq!(
            output.defaultspack_lock_sha256,
            byte_digest(&fs::read(bundle_root.join("bundle.lock.json")).unwrap())
        );
        assert!(artifact_root.join("Tobkiri.AppImage").is_file());
    }

    #[test]
    fn verify_rejects_missing_or_wrongly_named_catalog() {
        let tree = TestTree::new("missing-catalog");
        let source_root = tree.path().join("source");
        let staged_root = tree.path().join("staged").join("bundled");
        fs::create_dir_all(&source_root).expect("source should be creatable");
        fs::create_dir_all(&staged_root).expect("staged should be creatable");
        fs::write(source_root.join("wrong_filename.json"), b"catalog")
            .expect("wrongly named catalog should be writable");
        let staged_catalog = staged_root.join(PRESENTATION_CATALOG_FILENAME);
        fs::write(&staged_catalog, b"catalog").expect("staged catalog should be writable");

        let source_catalog = source_root.join(PRESENTATION_CATALOG_FILENAME);
        let error = verify_staged_catalog(&source_catalog, &staged_catalog)
            .expect_err("missing exact catalog filename must fail");
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
        assert!(error.to_string().contains(PRESENTATION_CATALOG_FILENAME));
    }

    #[test]
    fn verify_rejects_catalog_directory_substitution() {
        let tree = TestTree::new("directory-substitution");
        let source_catalog = tree
            .path()
            .join("source")
            .join(PRESENTATION_CATALOG_FILENAME);
        let staged_catalog = tree
            .path()
            .join("staged")
            .join(PRESENTATION_CATALOG_FILENAME);
        fs::create_dir_all(source_catalog.parent().expect("source has a parent"))
            .expect("source should be creatable");
        fs::create_dir_all(staged_catalog.parent().expect("staged has a parent"))
            .expect("staged should be creatable");
        fs::create_dir_all(&source_catalog).expect("source directory substitution should work");
        fs::write(&staged_catalog, b"catalog").expect("staged catalog should be writable");

        let error = verify_staged_catalog(&source_catalog, &staged_catalog)
            .expect_err("source directory substitution must fail");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);

        fs::remove_dir(&source_catalog).expect("source directory should be removable");
        fs::write(&source_catalog, b"catalog").expect("source catalog should be writable");
        fs::remove_file(&staged_catalog).expect("staged catalog should be removable");
        fs::create_dir(&staged_catalog).expect("staged directory substitution should work");

        let error = verify_staged_catalog(&source_catalog, &staged_catalog)
            .expect_err("staged directory substitution must fail");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn verify_rejects_catalog_digest_mismatch() {
        let tree = TestTree::new("tampered-catalog");
        let source_catalog = tree
            .path()
            .join("source")
            .join(PRESENTATION_CATALOG_FILENAME);
        let staged_catalog = tree
            .path()
            .join("staged")
            .join(PRESENTATION_CATALOG_FILENAME);
        fs::create_dir_all(source_catalog.parent().expect("source has a parent"))
            .expect("source should be creatable");
        fs::create_dir_all(staged_catalog.parent().expect("staged has a parent"))
            .expect("staged should be creatable");
        fs::write(&source_catalog, br#"{"artifact":{"sha256":"sha256:good"}}"#)
            .expect("source catalog should be writable");
        fs::write(&staged_catalog, br#"{"artifact":{"sha256":"sha256:bad"}}"#)
            .expect("staged catalog should be writable");

        let error = verify_staged_catalog(&source_catalog, &staged_catalog)
            .expect_err("catalog digest mismatch must fail closed");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("differs"));
    }

    #[cfg(unix)]
    #[test]
    fn stage_rejects_symlinked_catalog_and_artifact_paths() {
        let tree = TestTree::new("symlink-paths");
        let (release_root, staged_root, catalog) = release_fixture(&tree);
        let valid_catalog = fs::read(&catalog).expect("fixture catalog should be readable");
        let outside_catalog = tree.path().join("outside-catalog.json");
        fs::write(&outside_catalog, b"outside catalog").expect("outside catalog should exist");
        fs::remove_file(&catalog).expect("fixture catalog should be removable");
        std::os::unix::fs::symlink(&outside_catalog, &catalog)
            .expect("catalog symlink should be creatable");

        let error = stage_presentation_release_at(&staged_root, &release_root)
            .expect_err("symlinked release catalog must fail closed");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);

        fs::remove_file(&catalog).expect("catalog symlink should be removable");
        fs::write(&catalog, valid_catalog).expect("catalog should be restorable");
        let outside_artifact = tree.path().join("outside-shell");
        fs::write(&outside_artifact, b"outside shell").expect("outside artifact should exist");
        let artifact_link = release_root
            .join("bundled")
            .join("presentation-artifacts")
            .join("escaped-shell");
        std::os::unix::fs::symlink(&outside_artifact, &artifact_link)
            .expect("artifact symlink should be creatable");

        let error = stage_presentation_release_at(&staged_root, &release_root)
            .expect_err("symlinked artifact must fail closed");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("symlink"));
    }

    #[cfg(unix)]
    #[test]
    fn stage_rejects_release_root_symlink_path_escape() {
        let tree = TestTree::new("release-root-escape");
        let (release_root, staged_root, _) = release_fixture(&tree);
        let release_link = tree.path().join("release-link");
        std::os::unix::fs::symlink(&release_root, &release_link)
            .expect("release root symlink should be creatable");

        let error = stage_presentation_release_at(&staged_root, &release_link)
            .expect_err("release root symlink must fail closed");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("release presentation root"));
    }
}
