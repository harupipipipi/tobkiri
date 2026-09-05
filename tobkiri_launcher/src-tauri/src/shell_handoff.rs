//! One-shot Launcher -> presentation Shell runtime handoff.
//!
//! The authenticated runtime URL is never placed in argv or the environment.
//! The Launcher writes it to a short-lived owner-only file below its own app
//! data root and passes only that file path to the verified Shell process. The
//! Shell atomically claims, validates, reads, and removes the file before
//! navigating its WebView. Binding admission is reported separately through a
//! nonce-bound, owner-only atomic receipt; it does not report page readiness,
//! and file disappearance is never admission.

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{anyhow, bail, Context, Result};
use rand::{distributions::Alphanumeric, Rng};
use serde::{Deserialize, Serialize};
use tauri::Url;

use crate::config::AppConfig;
use crate::host_contract::ExecutionProfileIdentity;

/// Bundle identifier used only to select the presentation-only Shell runtime.
pub(crate) const SHELL_BUNDLE_IDENTIFIER: &str = "io.tobkiri.shell.tauri";
pub(crate) const HANDOFF_ARGUMENT: &str = "--tobkiri-shell-handoff";
const LAUNCHER_BUNDLE_IDENTIFIER: &str = "dev.rumiai.app";
const CI_E2E_LAUNCHER_BUNDLE_IDENTIFIER: &str = "dev.tobkiri.launcher.ci-e2e";
const MACOS_ARTIFACT_POLICY: &str = env!("TOBKIRI_MACOS_ARTIFACT_POLICY");
// v3 existed only in the immediately preceding, unmerged Launcher selector
// commit: no branch, tag, signed catalog, or release artifact contained it.
// v4 is therefore a hard internal transition, not a shipped compatibility cut.
const HANDOFF_SCHEMA: &str = "io.tobkiri.shell-handoff.v4";
const RECEIPT_SCHEMA: &str = "io.tobkiri.shell-handoff-ack.v1";
const LOCAL_AUTH_PROTOCOL: &str = "io.tobkiri.local-auth.v1";
const LOCAL_AUTH_AUDIENCE: &str = "runtime-profile";
const HANDOFF_DIRECTORY: &str = "shell_handoff";
const HANDOFF_TTL_SECONDS: u64 = 60;
const HANDOFF_MAX_LIFETIME_SECONDS: u64 = 120;
const HANDOFF_MAX_BYTES: u64 = 16 * 1024;
const RECEIPT_MAX_BYTES: u64 = 1024;
const STALE_CLEANUP_MAX_ENTRIES: usize = 256;
#[cfg(any(windows, test))]
const WINDOWS_FILE_ALL_ACCESS: u32 = 0x001f_01ff;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ShellHandoffPayload {
    schema: String,
    protocol: String,
    audience: String,
    profile_id: String,
    profile_revision: String,
    activation_id: String,
    plan_digest: String,
    catalog_revision: String,
    provider_id: String,
    artifact_id: String,
    artifact_digest: String,
    entrypoint_digest: String,
    runtime_url: String,
    created_at: u64,
    expires_at: u64,
    nonce: String,
    receipt_nonce: String,
}

pub(crate) struct ValidatedShellHandoff {
    pub runtime_url: Url,
    pub runtime_port: u16,
    pub identity: ExecutionProfileIdentity,
    pub catalog_revision: String,
    pub artifact: ShellArtifactIdentity,
    pub receipt: ShellHandoffReceiptIdentity,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ShellHandoffTicket {
    pub path: PathBuf,
    pub receipt: ShellHandoffReceiptIdentity,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ShellHandoffReceiptIdentity {
    pub(crate) root: PathBuf,
    pub(crate) handoff_nonce: String,
    pub(crate) receipt_nonce: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ShellHandoffReceiptStatus {
    /// The verified Shell process irreversibly admitted the exact Profile,
    /// Activation, Plan, catalog, artifact, and runtime binding. This does not
    /// assert that bootstrap, session setup, or page loading is complete.
    BindingAdmitted,
    RotationRequired,
}

impl ShellHandoffReceiptStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::BindingAdmitted => "binding_admitted",
            Self::RotationRequired => "rotation_required",
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ShellHandoffReceiptPayload {
    schema: String,
    handoff_nonce: String,
    receipt_nonce: String,
    status: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ShellArtifactIdentity {
    pub provider_id: String,
    pub artifact_id: String,
    pub artifact_digest: String,
    pub entrypoint_digest: String,
}

impl ShellArtifactIdentity {
    pub(crate) fn matches(&self, other: &Self) -> bool {
        self == other
    }
}

pub(crate) struct ShellHandoffBinding<'a> {
    pub identity: &'a ExecutionProfileIdentity,
    pub catalog_revision: &'a str,
    pub provider_id: &'a str,
    pub artifact_id: &'a str,
    pub artifact_digest: &'a str,
    pub entrypoint_digest: &'a str,
}

pub(crate) fn create_shell_handoff(
    config: &AppConfig,
    binding: ShellHandoffBinding<'_>,
    runtime_url: &str,
) -> Result<ShellHandoffTicket> {
    let root = launcher_handoff_root(config)?;
    prepare_private_root(&root)?;
    cleanup_stale_handoffs(&root);

    let now = epoch_seconds()?;
    let nonce = random_component(40);
    let receipt_nonce = random_component(40);
    let payload = ShellHandoffPayload {
        schema: HANDOFF_SCHEMA.to_string(),
        protocol: LOCAL_AUTH_PROTOCOL.to_string(),
        audience: LOCAL_AUTH_AUDIENCE.to_string(),
        profile_id: binding.identity.profile_id.clone(),
        profile_revision: binding.identity.profile_revision.clone(),
        activation_id: binding.identity.activation_id.clone(),
        plan_digest: binding.identity.plan_digest.clone(),
        catalog_revision: binding.catalog_revision.to_string(),
        provider_id: binding.provider_id.to_string(),
        artifact_id: binding.artifact_id.to_string(),
        artifact_digest: binding.artifact_digest.to_string(),
        entrypoint_digest: binding.entrypoint_digest.to_string(),
        runtime_url: runtime_url.to_string(),
        created_at: now,
        expires_at: now.saturating_add(HANDOFF_TTL_SECONDS),
        nonce: nonce.clone(),
        receipt_nonce: receipt_nonce.clone(),
    };
    validate_payload(&payload, now, &root)?;

    let body = serde_json::to_vec(&payload).context("failed to encode Shell handoff")?;
    if body.len() as u64 > HANDOFF_MAX_BYTES {
        bail!("Shell handoff exceeds the bounded payload size");
    }
    let path = root.join(format!("handoff-{nonce}.json"));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&path)
        .with_context(|| format!("failed to create Shell handoff at {}", path.display()))?;
    let result = (|| -> Result<()> {
        // Restrict the newly-created empty file before placing the
        // authenticated runtime URL in it. The private parent prevents
        // replacement while the path-based Windows DACL operation runs.
        restrict_private_file(&path)?;
        file.write_all(&body)
            .context("failed to write Shell handoff")?;
        file.sync_all().context("failed to flush Shell handoff")?;
        validate_private_file(&path)
    })();
    drop(file);
    if let Err(error) = result {
        let _ = fs::remove_file(&path);
        return Err(error);
    }
    Ok(ShellHandoffTicket {
        path,
        receipt: ShellHandoffReceiptIdentity {
            root,
            handoff_nonce: nonce,
            receipt_nonce,
        },
    })
}

pub(crate) fn discard_shell_handoff(ticket: &ShellHandoffTicket) {
    let _ = fs::remove_file(&ticket.path);
    let _ = fs::remove_file(receipt_path(&ticket.receipt));
}

pub(crate) fn consume_shell_handoff(path: &Path) -> Result<ValidatedShellHandoff> {
    let expected_root = expected_launcher_handoff_root()?;
    consume_shell_handoff_from_root(path, &expected_root)
}

fn consume_shell_handoff_from_root(
    path: &Path,
    expected_root: &Path,
) -> Result<ValidatedShellHandoff> {
    validate_private_root(expected_root)?;
    let handoff_nonce = validate_handoff_path(path, expected_root)?;

    let claimed = expected_root.join(format!(".consume-{}.json", random_component(40)));
    fs::rename(path, &claimed).context("failed to claim Shell handoff")?;

    let result = (|| {
        validate_claimed_file(&claimed, expected_root)?;
        let file = open_private_file_no_follow(&claimed)
            .context("failed to open claimed Shell handoff")?;
        let mut body = Vec::new();
        file.take(HANDOFF_MAX_BYTES + 1)
            .read_to_end(&mut body)
            .context("failed to read Shell handoff")?;
        if body.len() as u64 > HANDOFF_MAX_BYTES {
            bail!("Shell handoff exceeds the bounded payload size");
        }
        let payload: ShellHandoffPayload =
            serde_json::from_slice(&body).context("Shell handoff is malformed")?;
        if payload.nonce != handoff_nonce {
            bail!("Shell handoff filename nonce does not match its authenticated payload");
        }
        validate_payload(&payload, epoch_seconds()?, expected_root)
    })();
    let _ = fs::remove_file(&claimed);
    result
}

pub(crate) fn write_shell_handoff_receipt(
    identity: &ShellHandoffReceiptIdentity,
    status: ShellHandoffReceiptStatus,
) -> Result<()> {
    validate_private_root(&identity.root)?;
    validate_nonce(&identity.handoff_nonce, "handoff receipt handoff nonce")?;
    validate_nonce(&identity.receipt_nonce, "handoff receipt nonce")?;
    let payload = ShellHandoffReceiptPayload {
        schema: RECEIPT_SCHEMA.to_string(),
        handoff_nonce: identity.handoff_nonce.clone(),
        receipt_nonce: identity.receipt_nonce.clone(),
        status: status.as_str().to_string(),
    };
    let body = serde_json::to_vec(&payload).context("failed to encode Shell handoff receipt")?;
    if body.len() as u64 > RECEIPT_MAX_BYTES {
        bail!("Shell handoff receipt exceeds the bounded payload size");
    }

    let temporary = identity.root.join(format!(
        ".receipt-{}-{}.tmp",
        identity.receipt_nonce,
        random_component(40)
    ));
    let final_path = receipt_path(identity);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&temporary)
        .context("failed to create Shell handoff receipt")?;
    let mut published = false;
    let result = (|| -> Result<()> {
        restrict_private_file(&temporary)?;
        file.write_all(&body)
            .context("failed to write Shell handoff receipt")?;
        file.sync_all()
            .context("failed to flush Shell handoff receipt")?;
        validate_private_file(&temporary)?;
        // A hard link publishes the complete receipt atomically and refuses to
        // replace a pre-existing receipt, which makes replay fail closed.
        fs::hard_link(&temporary, &final_path)
            .context("failed to publish Shell handoff receipt atomically")?;
        published = true;
        validate_private_file(&final_path)
    })();
    drop(file);
    let _ = fs::remove_file(&temporary);
    if result.is_err() && published {
        let _ = fs::remove_file(&final_path);
    }
    result
}

pub(crate) fn consume_shell_handoff_receipt(
    ticket: &ShellHandoffTicket,
) -> Result<ShellHandoffReceiptStatus> {
    consume_shell_handoff_receipt_from_root(ticket, &ticket.receipt.root)
}

pub(crate) fn try_consume_shell_handoff_receipt(
    ticket: &ShellHandoffTicket,
) -> Result<Option<ShellHandoffReceiptStatus>> {
    let path = receipt_path(&ticket.receipt);
    match fs::symlink_metadata(&path) {
        Ok(_) => consume_shell_handoff_receipt(ticket).map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).context("failed to inspect Shell handoff receipt"),
    }
}

fn consume_shell_handoff_receipt_from_root(
    ticket: &ShellHandoffTicket,
    expected_root: &Path,
) -> Result<ShellHandoffReceiptStatus> {
    validate_private_root(expected_root)?;
    if ticket.receipt.root != expected_root {
        bail!("Shell handoff receipt root does not match its ticket");
    }
    let receipt = receipt_path(&ticket.receipt);
    let receipt_nonce = validate_receipt_path(&receipt, expected_root)?;
    if receipt_nonce != ticket.receipt.receipt_nonce {
        bail!("Shell handoff receipt filename nonce does not match its ticket");
    }
    let claimed = expected_root.join(format!(".consume-receipt-{}.json", random_component(40)));
    fs::rename(&receipt, &claimed).context("failed to claim Shell handoff receipt")?;
    let result = (|| {
        validate_claimed_file(&claimed, expected_root)?;
        let file = open_private_file_no_follow(&claimed)
            .context("failed to open claimed Shell handoff receipt")?;
        let mut body = Vec::new();
        file.take(RECEIPT_MAX_BYTES + 1)
            .read_to_end(&mut body)
            .context("failed to read Shell handoff receipt")?;
        if body.len() as u64 > RECEIPT_MAX_BYTES {
            bail!("Shell handoff receipt exceeds the bounded payload size");
        }
        let payload: ShellHandoffReceiptPayload =
            serde_json::from_slice(&body).context("Shell handoff receipt is malformed")?;
        validate_receipt_payload(&payload, &ticket.receipt)
    })();
    let _ = fs::remove_file(&claimed);
    result
}

pub(crate) fn handoff_path_from_os_args<I>(args: I) -> Result<PathBuf>
where
    I: IntoIterator<Item = OsString>,
{
    let values = args.into_iter().collect::<Vec<_>>();
    let mut found: Option<PathBuf> = None;
    let mut index = 0;
    while index < values.len() {
        if values[index] == HANDOFF_ARGUMENT {
            if found.is_some() {
                bail!("Shell handoff argument is duplicated");
            }
            let value = values
                .get(index + 1)
                .context("Shell handoff argument has no path")?;
            if value.is_empty() {
                bail!("Shell handoff path is empty");
            }
            found = Some(PathBuf::from(value));
            index += 2;
        } else {
            index += 1;
        }
    }
    found.context("Shell handoff argument is required")
}

pub(crate) fn handoff_path_from_strings(args: &[String]) -> Result<PathBuf> {
    handoff_path_from_os_args(args.iter().map(OsString::from))
}

fn launcher_handoff_root(config: &AppConfig) -> Result<PathBuf> {
    #[cfg(debug_assertions)]
    if MACOS_ARTIFACT_POLICY == "production-v1" && config.dev_workspace_root.is_some() {
        return development_launcher_handoff_root();
    }
    Ok(config.user_data_dir.join(HANDOFF_DIRECTORY))
}

#[cfg(debug_assertions)]
fn development_launcher_handoff_root() -> Result<PathBuf> {
    // LaunchServices deliberately does not preserve the Launcher's TMPDIR.
    // Both independently launched app processes can, however, resolve the
    // same per-user cache directory without inherited environment state.
    let cache_dir = dirs::cache_dir().context("platform cache directory is unavailable")?;
    Ok(cache_dir
        .join(LAUNCHER_BUNDLE_IDENTIFIER)
        .join("development")
        .join(HANDOFF_DIRECTORY))
}

fn expected_launcher_handoff_root() -> Result<PathBuf> {
    // App-bundled debug Launchers deliberately keep rebuild-specific writable
    // state out of Application Support.  The development Shell is launched by
    // LaunchServices, so it cannot inherit the Launcher's environment; derive
    // the same bounded root that AppConfig uses instead.  File ownership,
    // permissions, path containment, nonce, lifetime, and payload identity are
    // still verified below. Packaged production and CI policies remain bound
    // to their Application Support container.
    #[cfg(debug_assertions)]
    if MACOS_ARTIFACT_POLICY == "production-v1" {
        return development_launcher_handoff_root();
    }
    let data_dir = dirs::data_dir().context("platform data directory is unavailable")?;
    let launcher_bundle_identifier =
        launcher_bundle_identifier_for_artifact_policy(MACOS_ARTIFACT_POLICY)?;
    Ok(data_dir
        .join(launcher_bundle_identifier)
        .join("user_data")
        .join(HANDOFF_DIRECTORY))
}

fn launcher_bundle_identifier_for_artifact_policy(policy: &str) -> Result<&'static str> {
    match policy {
        "production-v1" => Ok(LAUNCHER_BUNDLE_IDENTIFIER),
        "ci-e2e-v1" => Ok(CI_E2E_LAUNCHER_BUNDLE_IDENTIFIER),
        _ => bail!("Tobkiri Shell was built with an unsupported artifact policy"),
    }
}

fn is_clean_absolute_path(path: &Path) -> bool {
    path.is_absolute()
        && path.components().all(|component| {
            matches!(component, Component::RootDir | Component::Normal(_))
                || cfg!(windows) && matches!(component, Component::Prefix(_))
        })
}

fn validate_handoff_path(path: &Path, expected_root: &Path) -> Result<String> {
    if !is_clean_absolute_path(path) {
        bail!("Shell handoff path is not a clean absolute path");
    }
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .context("Shell handoff filename is invalid")?;
    let nonce = name
        .strip_prefix("handoff-")
        .and_then(|value| value.strip_suffix(".json"))
        .context("Shell handoff filename is invalid")?;
    if nonce.len() != 40 || !nonce.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
        bail!("Shell handoff filename is invalid");
    }
    let parent = path.parent().context("Shell handoff path has no parent")?;
    let expected = expected_root
        .canonicalize()
        .context("Launcher Shell handoff root is unavailable")?;
    let actual = parent
        .canonicalize()
        .context("Shell handoff parent is unavailable")?;
    if actual != expected {
        bail!("Shell handoff path is outside the Launcher-owned handoff root");
    }
    let metadata = fs::symlink_metadata(path).context("Shell handoff file is missing")?;
    if metadata.file_type().is_symlink() || windows_reparse_point(&metadata) || !metadata.is_file()
    {
        bail!("Shell handoff must be a regular non-reparse file");
    }
    Ok(nonce.to_string())
}

fn receipt_path(identity: &ShellHandoffReceiptIdentity) -> PathBuf {
    identity
        .root
        .join(format!("receipt-{}.json", identity.receipt_nonce))
}

fn validate_receipt_path(path: &Path, expected_root: &Path) -> Result<String> {
    if !is_clean_absolute_path(path) {
        bail!("Shell handoff receipt path is not a clean absolute path");
    }
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .context("Shell handoff receipt filename is invalid")?;
    let nonce = name
        .strip_prefix("receipt-")
        .and_then(|value| value.strip_suffix(".json"))
        .context("Shell handoff receipt filename is invalid")?;
    validate_nonce(nonce, "handoff receipt filename nonce")?;
    let parent = path
        .parent()
        .context("Shell handoff receipt path has no parent")?;
    let expected = expected_root
        .canonicalize()
        .context("Launcher Shell handoff root is unavailable")?;
    let actual = parent
        .canonicalize()
        .context("Shell handoff receipt parent is unavailable")?;
    if actual != expected {
        bail!("Shell handoff receipt is outside the Launcher-owned handoff root");
    }
    validate_private_file(path)?;
    Ok(nonce.to_string())
}

fn validate_claimed_file(path: &Path, expected_root: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path).context("claimed Shell handoff is missing")?;
    if metadata.file_type().is_symlink() || windows_reparse_point(&metadata) || !metadata.is_file()
    {
        bail!("claimed Shell handoff must be a regular non-reparse file");
    }
    let root = expected_root
        .canonicalize()
        .context("failed to canonicalize Shell handoff root")?;
    let parent = path
        .parent()
        .context("claimed Shell handoff has no parent")?
        .canonicalize()
        .context("failed to canonicalize claimed Shell handoff parent")?;
    if parent != root {
        bail!("claimed Shell handoff escaped its private root");
    }
    validate_private_file(path)
}

fn validate_payload(
    payload: &ShellHandoffPayload,
    now: u64,
    root: &Path,
) -> Result<ValidatedShellHandoff> {
    if payload.schema != HANDOFF_SCHEMA
        || payload.protocol != LOCAL_AUTH_PROTOCOL
        || payload.audience != LOCAL_AUTH_AUDIENCE
    {
        bail!("Shell handoff identity is invalid");
    }
    let identity = ExecutionProfileIdentity::new(
        payload.profile_id.clone(),
        payload.profile_revision.clone(),
        payload.activation_id.clone(),
        payload.plan_digest.clone(),
    )
    .context("Shell handoff execution Profile identity is invalid")?;
    validate_sha256(&payload.catalog_revision, "catalog revision")?;
    if !valid_artifact_identifier(&payload.provider_id)
        || !valid_artifact_identifier(&payload.artifact_id)
    {
        bail!("Shell handoff artifact identity is invalid");
    }
    validate_sha256(&payload.artifact_digest, "artifact digest")?;
    validate_sha256(&payload.entrypoint_digest, "entrypoint digest")?;
    validate_nonce(&payload.nonce, "handoff nonce")?;
    validate_nonce(&payload.receipt_nonce, "handoff receipt nonce")?;
    if payload.nonce == payload.receipt_nonce {
        bail!("Shell handoff and receipt nonces must be independent");
    }
    if payload.expires_at < payload.created_at
        || payload.expires_at.saturating_sub(payload.created_at) > HANDOFF_MAX_LIFETIME_SECONDS
        || payload.created_at > now.saturating_add(5)
        || now > payload.expires_at
    {
        bail!("Shell handoff is expired or has an invalid lifetime");
    }
    if payload.runtime_url.len() > 8192 {
        bail!("Shell runtime URL is too large");
    }
    let runtime_url = Url::parse(&payload.runtime_url).context("Shell runtime URL is invalid")?;
    if runtime_url.scheme() != "http"
        || runtime_url.host_str() != Some("127.0.0.1")
        || runtime_url.username() != ""
        || runtime_url.password().is_some()
        || runtime_url.fragment().is_some()
    {
        bail!("Shell runtime URL is outside the authenticated loopback contract");
    }
    crate::health_check::validate_application_route(runtime_url.path())
        .context("Shell runtime URL has an unsafe Application route")?;
    let runtime_port = runtime_url
        .port()
        .context("Shell runtime URL must use an explicit loopback port")?;
    if runtime_port == 0 {
        bail!("Shell runtime URL has an invalid port");
    }
    let mut query = runtime_url.query_pairs();
    let (key, code) = query
        .next()
        .context("Shell runtime URL is missing its one-time panel code")?;
    if key != "code"
        || !(32..=256).contains(&code.len())
        || !code
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
        || query.next().is_some()
    {
        bail!("Shell runtime URL has an invalid one-time panel code");
    }
    Ok(ValidatedShellHandoff {
        runtime_url,
        runtime_port,
        identity,
        catalog_revision: payload.catalog_revision.clone(),
        artifact: ShellArtifactIdentity {
            provider_id: payload.provider_id.clone(),
            artifact_id: payload.artifact_id.clone(),
            artifact_digest: payload.artifact_digest.clone(),
            entrypoint_digest: payload.entrypoint_digest.clone(),
        },
        receipt: ShellHandoffReceiptIdentity {
            root: root.to_path_buf(),
            handoff_nonce: payload.nonce.clone(),
            receipt_nonce: payload.receipt_nonce.clone(),
        },
    })
}

fn validate_nonce(value: &str, label: &str) -> Result<()> {
    if value.len() != 40 || !value.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
        bail!("Shell {label} is invalid");
    }
    Ok(())
}

fn validate_receipt_payload(
    payload: &ShellHandoffReceiptPayload,
    expected: &ShellHandoffReceiptIdentity,
) -> Result<ShellHandoffReceiptStatus> {
    if payload.schema != RECEIPT_SCHEMA
        || payload.handoff_nonce != expected.handoff_nonce
        || payload.receipt_nonce != expected.receipt_nonce
    {
        bail!("Shell handoff receipt identity is invalid");
    }
    match payload.status.as_str() {
        "binding_admitted" => Ok(ShellHandoffReceiptStatus::BindingAdmitted),
        "rotation_required" => Ok(ShellHandoffReceiptStatus::RotationRequired),
        _ => bail!("Shell handoff receipt status is invalid"),
    }
}

fn open_private_file_no_follow(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    options.open(path).map_err(Into::into)
}

fn valid_artifact_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn validate_sha256(value: &str, label: &str) -> Result<()> {
    let digest = value
        .strip_prefix("sha256:")
        .ok_or_else(|| anyhow!("Shell handoff {label} is invalid"))?;
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        bail!("Shell handoff {label} is invalid");
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn windows_private_security_sddl_is_valid(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("O:") else {
        return false;
    };
    let Some(dacl_start) = rest.find("D:") else {
        return false;
    };
    let owner = &rest[..dacl_start];
    if owner.is_empty() || owner == "OW" {
        return false;
    }
    let rest = &rest[dacl_start + 2..];
    let Some(ace_start) = rest.find('(') else {
        return false;
    };
    let mut flags = &rest[..ace_start];
    let mut protected = false;
    let mut auto_inherited = false;
    while !flags.is_empty() {
        if let Some(remaining) = flags.strip_prefix("AI") {
            if auto_inherited {
                return false;
            }
            auto_inherited = true;
            flags = remaining;
        } else if let Some(remaining) = flags.strip_prefix('P') {
            if protected {
                return false;
            }
            protected = true;
            flags = remaining;
        } else {
            return false;
        }
    }
    if !protected {
        return false;
    }

    let mut rest = &rest[ace_start..];
    let mut trustees = Vec::new();
    while !rest.is_empty() {
        if !rest.starts_with('(') {
            return false;
        }
        let Some(end) = rest.find(')') else {
            return false;
        };
        let fields = rest[1..end].split(';').collect::<Vec<_>>();
        if fields.len() != 6
            || fields[0] != "A"
            || !fields[1].is_empty()
            || !windows_sddl_rights_are_file_all_access(fields[2])
            || !fields[3].is_empty()
            || !fields[4].is_empty()
        {
            return false;
        }
        trustees.push(fields[5]);
        rest = &rest[end + 1..];
    }
    if trustees.len() != 2 {
        return false;
    }
    let mut actual = trustees
        .into_iter()
        .map(windows_canonical_sddl_trustee)
        .collect::<Vec<_>>();
    let mut expected = vec![
        windows_canonical_sddl_trustee(owner),
        windows_canonical_sddl_trustee("S-1-5-18"),
    ];
    actual.sort_unstable();
    expected.sort_unstable();
    actual == expected
}

#[cfg(any(windows, test))]
fn windows_sddl_rights_are_file_all_access(value: &str) -> bool {
    value == "FA"
        || value
            .strip_prefix("0x")
            .and_then(|mask| u32::from_str_radix(mask, 16).ok())
            == Some(WINDOWS_FILE_ALL_ACCESS)
}

#[cfg(any(windows, test))]
fn windows_canonical_sddl_trustee(value: &str) -> &str {
    match value {
        "SY" => "S-1-5-18",
        value => value,
    }
}

#[cfg(windows)]
fn windows_path_wide(path: &Path) -> Result<Vec<u16>> {
    use std::os::windows::ffi::OsStrExt;

    let mut value = path.as_os_str().encode_wide().collect::<Vec<_>>();
    if value.contains(&0) {
        bail!("Shell handoff path contains an embedded NUL");
    }
    value.push(0);
    Ok(value)
}

#[cfg(windows)]
struct WindowsLocalAllocation(*mut core::ffi::c_void);

#[cfg(windows)]
impl Drop for WindowsLocalAllocation {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                windows_sys::Win32::Foundation::LocalFree(self.0);
            }
        }
    }
}

#[cfg(windows)]
fn apply_windows_private_dacl(path: &Path) -> Result<()> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::Authorization::{
        ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
    };
    use windows_sys::Win32::Security::{
        SetFileSecurityW, DACL_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION,
    };

    let owner = windows_owner_sid(path)?;
    let sddl = format!("D:P(A;;FA;;;{owner})(A;;FA;;;SY)")
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let mut descriptor = null_mut();
    let converted = unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            null_mut(),
        )
    };
    if converted == 0 || descriptor.is_null() {
        return Err(std::io::Error::last_os_error())
            .context("failed to construct the private Windows Shell handoff DACL");
    }
    let _descriptor = WindowsLocalAllocation(descriptor);
    let wide = windows_path_wide(path)?;
    let applied = unsafe {
        SetFileSecurityW(
            wide.as_ptr(),
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        )
    };
    if applied == 0 {
        return Err(std::io::Error::last_os_error()).with_context(|| {
            format!(
                "failed to apply the private Windows Shell handoff DACL to {}",
                path.display()
            )
        });
    }
    Ok(())
}

#[cfg(windows)]
fn windows_owner_sid(path: &Path) -> Result<String> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::GetSecurityDescriptorOwner;
    use windows_sys::Win32::Security::OWNER_SECURITY_INFORMATION;

    let mut descriptor = windows_security_descriptor(path, OWNER_SECURITY_INFORMATION)?;
    let mut owner = null_mut();
    let mut defaulted = 0;
    let loaded = unsafe {
        GetSecurityDescriptorOwner(descriptor.as_mut_ptr().cast(), &mut owner, &mut defaulted)
    };
    if loaded == 0 || owner.is_null() {
        return Err(std::io::Error::last_os_error()).with_context(|| {
            format!(
                "failed to inspect the Windows owner SID for {}",
                path.display()
            )
        });
    }
    windows_sid_string(owner).with_context(|| {
        format!(
            "failed to translate the Windows owner SID for {}",
            path.display()
        )
    })
}

#[cfg(windows)]
fn windows_sid_string(sid: *mut core::ffi::c_void) -> Result<String> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;

    let mut text = null_mut();
    let converted = unsafe { ConvertSidToStringSidW(sid, &mut text) };
    if converted == 0 || text.is_null() {
        return Err(std::io::Error::last_os_error()).context("failed to translate Windows SID");
    }
    let _text = WindowsLocalAllocation(text.cast());
    let mut len = 0;
    while unsafe { *text.add(len) } != 0 {
        len += 1;
    }
    let utf16 = unsafe { std::slice::from_raw_parts(text, len) };
    String::from_utf16(utf16).context("Windows owner SID is not valid UTF-16")
}

#[cfg(windows)]
fn windows_security_descriptor(path: &Path, information: u32) -> Result<Vec<u8>> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::GetFileSecurityW;

    let wide = windows_path_wide(path)?;
    let mut needed = 0_u32;
    unsafe {
        GetFileSecurityW(wide.as_ptr(), information, null_mut(), 0, &mut needed);
    }
    if needed == 0 {
        return Err(std::io::Error::last_os_error()).with_context(|| {
            format!(
                "failed to size the Windows Shell handoff security descriptor for {}",
                path.display()
            )
        });
    }
    let mut descriptor = vec![0_u8; needed as usize];
    let loaded = unsafe {
        GetFileSecurityW(
            wide.as_ptr(),
            information,
            descriptor.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    };
    if loaded == 0 {
        return Err(std::io::Error::last_os_error()).with_context(|| {
            format!(
                "failed to read the Windows Shell handoff security descriptor for {}",
                path.display()
            )
        });
    }
    Ok(descriptor)
}

#[cfg(windows)]
fn validate_windows_private_dacl(path: &Path) -> Result<()> {
    if !windows_private_security_descriptor_is_valid(path)? {
        bail!(
            "Windows Shell handoff ACL is not owner-and-system-only for {}",
            path.display()
        );
    }
    Ok(())
}

#[cfg(windows)]
fn windows_private_security_descriptor_is_valid(path: &Path) -> Result<bool> {
    use std::mem::size_of;
    use std::ptr::{addr_of, null_mut};
    use windows_sys::Win32::Security::{
        AclSizeInformation, GetAce, GetAclInformation, GetSecurityDescriptorControl,
        GetSecurityDescriptorDacl, GetSecurityDescriptorOwner, ACCESS_ALLOWED_ACE,
        ACL_SIZE_INFORMATION, DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION,
        SE_DACL_PROTECTED,
    };

    let information = DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION;
    let mut descriptor = windows_security_descriptor(path, information)?;
    let descriptor_ptr = descriptor.as_mut_ptr().cast();
    let mut control = 0_u16;
    let mut _revision = 0_u32;
    if unsafe { GetSecurityDescriptorControl(descriptor_ptr, &mut control, &mut _revision) } == 0 {
        return Err(std::io::Error::last_os_error())
            .context("failed to inspect Windows security descriptor control flags");
    }
    if control & SE_DACL_PROTECTED == 0 {
        return Ok(false);
    }

    let mut owner = null_mut();
    let mut _owner_defaulted = 0;
    if unsafe { GetSecurityDescriptorOwner(descriptor_ptr, &mut owner, &mut _owner_defaulted) } == 0
        || owner.is_null()
    {
        return Err(std::io::Error::last_os_error())
            .context("failed to inspect Windows security descriptor owner");
    }
    let owner = windows_sid_string(owner)?;

    let mut dacl_present = 0;
    let mut dacl = null_mut();
    let mut _dacl_defaulted = 0;
    if unsafe {
        GetSecurityDescriptorDacl(
            descriptor_ptr,
            &mut dacl_present,
            &mut dacl,
            &mut _dacl_defaulted,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error())
            .context("failed to inspect Windows security descriptor DACL");
    }
    if dacl_present == 0 || dacl.is_null() {
        return Ok(false);
    }

    let mut size = ACL_SIZE_INFORMATION::default();
    if unsafe {
        GetAclInformation(
            dacl,
            (&mut size as *mut ACL_SIZE_INFORMATION).cast(),
            size_of::<ACL_SIZE_INFORMATION>() as u32,
            AclSizeInformation,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error()).context("failed to inspect Windows DACL size");
    }
    if size.AceCount != 2 {
        return Ok(false);
    }

    let mut trustees = Vec::with_capacity(2);
    for index in 0..size.AceCount {
        let mut raw_ace = null_mut();
        if unsafe { GetAce(dacl, index, &mut raw_ace) } == 0 || raw_ace.is_null() {
            return Err(std::io::Error::last_os_error())
                .context("failed to inspect Windows DACL ACE");
        }
        let ace = unsafe { &*(raw_ace.cast::<ACCESS_ALLOWED_ACE>()) };
        if ace.Header.AceType != 0
            || ace.Header.AceFlags != 0
            || usize::from(ace.Header.AceSize) < size_of::<ACCESS_ALLOWED_ACE>()
            || ace.Mask != WINDOWS_FILE_ALL_ACCESS
        {
            return Ok(false);
        }
        let sid = addr_of!(ace.SidStart).cast_mut().cast();
        trustees.push(windows_sid_string(sid)?);
    }
    trustees.sort_unstable();
    let mut expected = vec![owner, "S-1-5-18".to_string()];
    expected.sort_unstable();
    Ok(trustees == expected)
}

#[cfg(windows)]
fn windows_reparse_point(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
fn windows_reparse_point(_metadata: &fs::Metadata) -> bool {
    false
}

fn prepare_private_root(root: &Path) -> Result<()> {
    fs::create_dir_all(root)
        .with_context(|| format!("failed to create Shell handoff root {}", root.display()))?;
    let metadata = fs::symlink_metadata(root)
        .with_context(|| format!("failed to inspect Shell handoff root {}", root.display()))?;
    if metadata.file_type().is_symlink() || windows_reparse_point(&metadata) || !metadata.is_dir() {
        bail!("Shell handoff root must be a non-reparse directory");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(root, fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(windows)]
    apply_windows_private_dacl(root)?;
    validate_private_root(root)
}

fn validate_private_root(root: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(root)
        .with_context(|| format!("failed to inspect Shell handoff root {}", root.display()))?;
    if metadata.file_type().is_symlink() || windows_reparse_point(&metadata) || !metadata.is_dir() {
        bail!("Shell handoff root must be a non-reparse directory");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.mode() & 0o077 != 0 || metadata.uid() != unsafe { libc::geteuid() } {
            bail!("Shell handoff root ownership or permissions are invalid");
        }
    }
    #[cfg(windows)]
    validate_windows_private_dacl(root)?;
    Ok(())
}

fn restrict_private_file(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("failed to inspect Shell handoff file {}", path.display()))?;
    if metadata.file_type().is_symlink() || windows_reparse_point(&metadata) || !metadata.is_file()
    {
        bail!("Shell handoff must remain a regular non-reparse file");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    #[cfg(windows)]
    apply_windows_private_dacl(path)?;
    validate_private_file(path)
}

fn validate_private_file(path: &Path) -> Result<()> {
    let file_metadata = fs::symlink_metadata(path)
        .with_context(|| format!("failed to inspect Shell handoff file {}", path.display()))?;
    if file_metadata.file_type().is_symlink()
        || windows_reparse_point(&file_metadata)
        || !file_metadata.is_file()
    {
        bail!("Shell handoff must remain a regular non-reparse file");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let parent_metadata =
            fs::symlink_metadata(path.parent().context("Shell handoff file has no parent")?)?;
        if file_metadata.mode() & 0o077 != 0
            || file_metadata.uid() != parent_metadata.uid()
            || file_metadata.uid() != unsafe { libc::geteuid() }
        {
            bail!("Shell handoff ownership or permissions are invalid");
        }
    }
    #[cfg(windows)]
    validate_windows_private_dacl(path)?;
    Ok(())
}

fn cleanup_stale_handoffs(root: &Path) {
    let Ok(now) = SystemTime::now().duration_since(UNIX_EPOCH) else {
        return;
    };
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten().take(STALE_CLEANUP_MAX_ENTRIES) {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        let managed = (name.starts_with("handoff-")
            || name.starts_with("receipt-")
            || name.starts_with(".consume-")
            || name.starts_with(".receipt-"))
            && (name.ends_with(".json") || name.ends_with(".tmp"));
        if !managed {
            continue;
        }
        let Ok(metadata) = fs::symlink_metadata(&path) else {
            continue;
        };
        let stale = metadata
            .modified()
            .ok()
            .and_then(|modified| now.checked_sub(modified.duration_since(UNIX_EPOCH).ok()?))
            .is_some_and(|age| age.as_secs() > HANDOFF_MAX_LIFETIME_SECONDS * 2);
        if stale && (metadata.is_file() || metadata.file_type().is_symlink()) {
            let _ = fs::remove_file(path);
        }
    }
}

fn epoch_seconds() -> Result<u64> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .context("system clock is before the Unix epoch")?
        .as_secs())
}

fn random_component(length: usize) -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(length)
        .map(char::from)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "tobkiri-shell-handoff-{name}-{}-{unique}",
            std::process::id()
        ))
    }

    fn test_config(root: &Path) -> AppConfig {
        AppConfig {
            app_dir: root.to_path_buf(),
            rumi_home: root.to_path_buf(),
            python_dir: root.join("python"),
            uv_path: root.join("uv"),
            venv_dir: root.join("venv"),
            user_data_dir: root.join("user_data"),
            log_dir: root.join("logs"),
            kernel_port: 8765,
            dev_workspace_root: None,
        }
    }

    #[test]
    fn handoff_argument_contains_only_a_path() {
        let path =
            PathBuf::from("/private/tmp/handoff-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.json");
        let parsed = handoff_path_from_os_args([
            OsString::from("tobkiri-shell"),
            OsString::from(HANDOFF_ARGUMENT),
            path.as_os_str().to_owned(),
        ])
        .unwrap();
        assert_eq!(parsed, path);
        assert!(handoff_path_from_os_args([OsString::from("tobkiri-shell")]).is_err());
        assert!(handoff_path_from_os_args([
            OsString::from(HANDOFF_ARGUMENT),
            OsString::from("a"),
            OsString::from(HANDOFF_ARGUMENT),
            OsString::from("b"),
        ])
        .is_err());
    }

    #[test]
    fn windows_private_dacl_accepts_concrete_owner_and_system_in_any_order() {
        let owner = "S-1-5-21-100-200-300-1001";
        assert!(windows_private_security_sddl_is_valid(&format!(
            "O:{owner}D:P(A;;FA;;;{owner})(A;;FA;;;SY)"
        )));
        assert!(windows_private_security_sddl_is_valid(&format!(
            "O:{owner}D:P(A;;FA;;;S-1-5-18)(A;;FA;;;{owner})"
        )));
        assert!(windows_private_security_sddl_is_valid(
            "O:SYD:P(A;;FA;;;S-1-5-18)(A;;FA;;;SY)"
        ));
        assert!(windows_private_security_sddl_is_valid(&format!(
            "O:{owner}D:P(A;;0x001F01FF;;;{owner})(A;;0x1f01ff;;;SY)"
        )));
    }

    #[test]
    fn windows_private_dacl_accepts_pai_regardless_of_flag_order() {
        let owner = "S-1-5-21-100-200-300-1001";
        assert!(windows_private_security_sddl_is_valid(&format!(
            "O:{owner}D:PAI(A;;FA;;;{owner})(A;;FA;;;SY)"
        )));
        assert!(windows_private_security_sddl_is_valid(&format!(
            "O:{owner}D:AIP(A;;FA;;;{owner})(A;;FA;;;SY)"
        )));
    }

    #[test]
    fn windows_private_dacl_rejects_owner_rights_inheritance_and_extras() {
        let owner = "S-1-5-21-100-200-300-1001";
        for invalid in [
            format!("O:{owner}D:(A;;FA;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:AI(A;;FA;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:PAR(A;;FA;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;ID;FA;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;CI;FA;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;OI;FA;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;;FA;;;OW)(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;;FA;;;S-1-3-4)(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;;FA;;;{owner})(A;;FA;;;SY)(A;;FR;;;WD)"),
            format!("O:{owner}D:P(A;;FR;;;{owner})(A;;FA;;;SY)"),
            format!("O:{owner}D:P(A;;FA;;;{owner})(A;;FA;;;WD)"),
            "O:SYD:P(A;;FA;;;SY)(A;;FA;;;WD)".to_string(),
        ] {
            assert!(
                !windows_private_security_sddl_is_valid(&invalid),
                "accepted {invalid}"
            );
        }
    }

    #[test]
    fn launcher_handoff_root_is_bound_to_the_artifact_policy() {
        assert_eq!(
            launcher_bundle_identifier_for_artifact_policy("production-v1").unwrap(),
            LAUNCHER_BUNDLE_IDENTIFIER
        );
        assert_eq!(
            launcher_bundle_identifier_for_artifact_policy("ci-e2e-v1").unwrap(),
            CI_E2E_LAUNCHER_BUNDLE_IDENTIFIER
        );
        assert!(launcher_bundle_identifier_for_artifact_policy("unknown").is_err());
    }

    #[test]
    fn handoff_ticket_binds_independent_handoff_and_receipt_nonces() {
        let root = temp_root("ticket-nonces");
        let config = test_config(&root);
        let identity = ExecutionProfileIdentity::new(
            "profile-a",
            format!("sha256:{}", "a".repeat(64)),
            "activation:profile-a-2026",
            format!("sha256:{}", "b".repeat(64)),
        )
        .unwrap();
        let ticket = create_shell_handoff(
            &config,
            ShellHandoffBinding {
                identity: &identity,
                catalog_revision: &format!("sha256:{}", "c".repeat(64)),
                provider_id: "fixture.shell",
                artifact_id: "fixture.shell.macos-arm64",
                artifact_digest: &format!("sha256:{}", "d".repeat(64)),
                entrypoint_digest: &format!("sha256:{}", "e".repeat(64)),
            },
            &format!("http://127.0.0.1:8766/?code={}", "f".repeat(64)),
        )
        .unwrap();
        let payload: ShellHandoffPayload =
            serde_json::from_slice(&fs::read(&ticket.path).unwrap()).unwrap();
        assert_eq!(payload.nonce, ticket.receipt.handoff_nonce);
        assert_eq!(payload.receipt_nonce, ticket.receipt.receipt_nonce);
        assert_ne!(payload.nonce, payload.receipt_nonce);
        discard_shell_handoff(&ticket);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn payload_rejects_wrong_identity_expiry_and_non_loopback_url() {
        let now = epoch_seconds().unwrap();
        let base = ShellHandoffPayload {
            schema: HANDOFF_SCHEMA.into(),
            protocol: LOCAL_AUTH_PROTOCOL.into(),
            audience: LOCAL_AUTH_AUDIENCE.into(),
            profile_id: "profile-a".into(),
            profile_revision: format!("sha256:{}", "a".repeat(64)),
            activation_id: "activation:profile-a-2026".into(),
            plan_digest: format!("sha256:{}", "c".repeat(64)),
            catalog_revision: format!("sha256:{}", "b".repeat(64)),
            provider_id: "fixture.shell".into(),
            artifact_id: "fixture.shell.macos-arm64".into(),
            artifact_digest: format!("sha256:{}", "d".repeat(64)),
            entrypoint_digest: format!("sha256:{}", "e".repeat(64)),
            runtime_url: format!("http://127.0.0.1:8766/?code={}", "c".repeat(64)),
            created_at: now,
            expires_at: now + 60,
            nonce: "A".repeat(40),
            receipt_nonce: "B".repeat(40),
        };
        let root = std::env::temp_dir();
        assert!(validate_payload(&base, now, &root).is_ok());
        let mut wrong_schema = serde_json::to_value(&base).unwrap();
        wrong_schema["schema"] = serde_json::Value::String("wrong".into());
        assert!(
            validate_payload(&serde_json::from_value(wrong_schema).unwrap(), now, &root).is_err()
        );
        let mut reused_nonce = serde_json::to_value(&base).unwrap();
        reused_nonce["receipt_nonce"] = serde_json::Value::String("A".repeat(40));
        assert!(
            validate_payload(&serde_json::from_value(reused_nonce).unwrap(), now, &root).is_err()
        );
        let mut wrong_provider = serde_json::to_value(&base).unwrap();
        wrong_provider["provider_id"] = serde_json::Value::String("../wrong.shell".into());
        assert!(
            validate_payload(&serde_json::from_value(wrong_provider).unwrap(), now, &root).is_err()
        );
        let mut external = serde_json::to_value(&base).unwrap();
        external["runtime_url"] = serde_json::Value::String(format!(
            "https://example.com/workspace?code={}",
            "c".repeat(64)
        ));
        assert!(validate_payload(&serde_json::from_value(external).unwrap(), now, &root).is_err());
        for invalid_url in [
            "http://127.0.0.1:8766/#rumi_local_auth=legacy-token".to_string(),
            "http://127.0.0.1:8766/?code=short".to_string(),
            format!("http://127.0.0.1:8766/?code={}&extra=1", "c".repeat(64)),
        ] {
            let mut invalid = serde_json::to_value(&base).unwrap();
            invalid["runtime_url"] = serde_json::Value::String(invalid_url);
            assert!(
                validate_payload(&serde_json::from_value(invalid).unwrap(), now, &root).is_err()
            );
        }
        let mut expired = serde_json::to_value(&base).unwrap();
        expired["expires_at"] = serde_json::Value::from(now.saturating_sub(1));
        assert!(validate_payload(&serde_json::from_value(expired).unwrap(), now, &root).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn private_file_validation_rejects_broad_mode_and_symlink() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let root = temp_root("permissions");
        fs::create_dir_all(&root).unwrap();
        prepare_private_root(&root).unwrap();
        let file = root.join("handoff-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.json");
        fs::write(&file, b"{}").unwrap();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(validate_private_file(&file).is_err());
        restrict_private_file(&file).unwrap();
        assert_eq!(
            fs::symlink_metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );

        let outside = root.join("outside.json");
        fs::write(&outside, b"{}").unwrap();
        let link = root.join("handoff-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB.json");
        symlink(&outside, &link).unwrap();
        assert!(validate_private_file(&link).is_err());
        assert!(restrict_private_file(&link).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn handoff_is_consumed_exactly_once_and_removed() {
        let root = temp_root("consume-once");
        fs::create_dir_all(&root).unwrap();
        prepare_private_root(&root).unwrap();
        let now = epoch_seconds().unwrap();
        let mut payload = ShellHandoffPayload {
            schema: HANDOFF_SCHEMA.into(),
            protocol: LOCAL_AUTH_PROTOCOL.into(),
            audience: LOCAL_AUTH_AUDIENCE.into(),
            profile_id: "profile-a".into(),
            profile_revision: format!("sha256:{}", "a".repeat(64)),
            activation_id: "activation:profile-a-2026".into(),
            plan_digest: format!("sha256:{}", "c".repeat(64)),
            catalog_revision: format!("sha256:{}", "b".repeat(64)),
            provider_id: "fixture.shell".into(),
            artifact_id: "fixture.shell.macos-arm64".into(),
            artifact_digest: format!("sha256:{}", "d".repeat(64)),
            entrypoint_digest: format!("sha256:{}", "e".repeat(64)),
            runtime_url: format!("http://127.0.0.1:8766/?code={}", "c".repeat(64)),
            created_at: now,
            expires_at: now + HANDOFF_TTL_SECONDS,
            nonce: "C".repeat(40),
            receipt_nonce: "R".repeat(40),
        };
        let path = root.join("handoff-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC.json");
        fs::write(&path, serde_json::to_vec(&payload).unwrap()).unwrap();
        restrict_private_file(&path).unwrap();
        #[cfg(windows)]
        {
            validate_windows_private_dacl(&root).unwrap();
            validate_windows_private_dacl(&path).unwrap();
        }

        let consumed = consume_shell_handoff_from_root(&path, &root).unwrap();
        assert_eq!(consumed.runtime_port, 8766);
        assert_eq!(consumed.identity.profile_id, "profile-a");
        assert_eq!(
            consumed.identity.profile_revision,
            format!("sha256:{}", "a".repeat(64))
        );
        assert_eq!(consumed.identity.activation_id, "activation:profile-a-2026");
        assert_eq!(
            consumed.identity.plan_digest,
            format!("sha256:{}", "c".repeat(64))
        );
        assert_eq!(
            consumed.catalog_revision,
            format!("sha256:{}", "b".repeat(64))
        );
        assert!(!path.exists());
        assert!(consume_shell_handoff_from_root(&path, &root).is_err());

        payload.nonce = "D".repeat(40);
        let mismatched = root.join("handoff-EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE.json");
        fs::write(&mismatched, serde_json::to_vec(&payload).unwrap()).unwrap();
        restrict_private_file(&mismatched).unwrap();
        let error = match consume_shell_handoff_from_root(&mismatched, &root) {
            Ok(_) => panic!("mismatched handoff filename nonce was accepted"),
            Err(error) => error.to_string(),
        };
        assert!(error.contains("filename nonce"));
        assert!(!mismatched.exists());

        assert!(fs::read_dir(&root)
            .unwrap()
            .filter_map(Result::ok)
            .all(|entry| !entry.file_name().to_string_lossy().starts_with(".consume-")));
        fs::remove_dir_all(root).unwrap();
    }

    fn receipt_ticket(root: &Path) -> ShellHandoffTicket {
        ShellHandoffTicket {
            path: root.join(format!("handoff-{}.json", "H".repeat(40))),
            receipt: ShellHandoffReceiptIdentity {
                root: root.to_path_buf(),
                handoff_nonce: "H".repeat(40),
                receipt_nonce: "R".repeat(40),
            },
        }
    }

    fn write_receipt_fixture(ticket: &ShellHandoffTicket, payload: serde_json::Value) {
        let path = receipt_path(&ticket.receipt);
        fs::write(&path, serde_json::to_vec(&payload).unwrap()).unwrap();
        restrict_private_file(&path).unwrap();
    }

    #[test]
    fn receipt_roundtrip_is_bounded_and_one_shot_for_both_statuses() {
        for status in [
            ShellHandoffReceiptStatus::BindingAdmitted,
            ShellHandoffReceiptStatus::RotationRequired,
        ] {
            let root = temp_root(status.as_str());
            prepare_private_root(&root).unwrap();
            let ticket = receipt_ticket(&root);
            write_shell_handoff_receipt(&ticket.receipt, status).unwrap();
            let body = fs::read(receipt_path(&ticket.receipt)).unwrap();
            let receipt: serde_json::Value = serde_json::from_slice(&body).unwrap();
            let keys = receipt
                .as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect::<std::collections::BTreeSet<_>>();
            assert_eq!(
                keys,
                ["handoff_nonce", "receipt_nonce", "schema", "status"]
                    .into_iter()
                    .map(str::to_string)
                    .collect()
            );
            assert_eq!(
                consume_shell_handoff_receipt_from_root(&ticket, &root).unwrap(),
                status
            );
            assert!(consume_shell_handoff_receipt_from_root(&ticket, &root).is_err());
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn receipt_rejects_schema_nonce_status_and_unknown_fields() {
        let cases = [
            serde_json::json!({
                "schema": "wrong",
                "handoff_nonce": "H".repeat(40),
                "receipt_nonce": "R".repeat(40),
                "status": "binding_admitted",
            }),
            serde_json::json!({
                "schema": RECEIPT_SCHEMA,
                "handoff_nonce": "X".repeat(40),
                "receipt_nonce": "R".repeat(40),
                "status": "binding_admitted",
            }),
            serde_json::json!({
                "schema": RECEIPT_SCHEMA,
                "handoff_nonce": "H".repeat(40),
                "receipt_nonce": "X".repeat(40),
                "status": "binding_admitted",
            }),
            serde_json::json!({
                "schema": RECEIPT_SCHEMA,
                "handoff_nonce": "H".repeat(40),
                "receipt_nonce": "R".repeat(40),
                "status": "failed",
            }),
            serde_json::json!({
                "schema": RECEIPT_SCHEMA,
                "handoff_nonce": "H".repeat(40),
                "receipt_nonce": "R".repeat(40),
                "status": "accepted",
            }),
            serde_json::json!({
                "schema": RECEIPT_SCHEMA,
                "handoff_nonce": "H".repeat(40),
                "receipt_nonce": "R".repeat(40),
                "status": "binding_admitted",
                "error": "must not be present",
            }),
        ];
        for (index, payload) in cases.into_iter().enumerate() {
            let root = temp_root(&format!("invalid-receipt-{index}"));
            prepare_private_root(&root).unwrap();
            let ticket = receipt_ticket(&root);
            write_receipt_fixture(&ticket, payload);
            assert!(consume_shell_handoff_receipt_from_root(&ticket, &root).is_err());
            assert!(!receipt_path(&ticket.receipt).exists());
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn receipt_rejects_symlink_and_broad_permissions() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let root = temp_root("receipt-permissions");
        prepare_private_root(&root).unwrap();
        let ticket = receipt_ticket(&root);
        let payload = serde_json::json!({
            "schema": RECEIPT_SCHEMA,
            "handoff_nonce": "H".repeat(40),
            "receipt_nonce": "R".repeat(40),
            "status": "binding_admitted",
        });
        write_receipt_fixture(&ticket, payload.clone());
        let path = receipt_path(&ticket.receipt);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(consume_shell_handoff_receipt_from_root(&ticket, &root).is_err());
        fs::remove_file(&path).unwrap();

        let outside = root.join("outside-receipt.json");
        fs::write(&outside, serde_json::to_vec(&payload).unwrap()).unwrap();
        symlink(&outside, &path).unwrap();
        assert!(consume_shell_handoff_receipt_from_root(&ticket, &root).is_err());
        fs::remove_file(path).unwrap();
        fs::remove_dir_all(root).unwrap();
    }
}
