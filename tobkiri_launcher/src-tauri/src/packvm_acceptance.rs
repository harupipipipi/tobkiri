//! Non-publishable native bridge for explicit PackVM sandbox acceptance.
//!
//! The socket is a control channel only.  It never manufactures guest evidence:
//! the supplied handler must use the normal authenticated Defaultspack contract
//! route, whose Broker owns Authority, admission, PackVM attestation, deadline,
//! cancellation, and cleanup.

#![cfg(unix)]

use std::fs;
use std::io::ErrorKind;
use std::io::{Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

const REQUEST_KIND: &str = "tobkiri.packvm.sandbox-acceptance.request.v1";
const MAX_REQUEST_BYTES: usize = 4096;
const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const SCENARIOS: [&str; 8] = [
    "probe_isolation",
    "stdin_overflow",
    "stdout_overflow",
    "stderr_overflow",
    "original_deadline",
    "cancel",
    "abnormal_exit",
    "resource_cleanup",
];

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct AcceptanceRequest {
    kind: String,
    pub(crate) scenario: String,
    pub(crate) nonce: String,
}

#[derive(Debug, Serialize)]
struct DiagnosticResponse<'a> {
    kind: &'static str,
    scenario: &'a str,
    nonce: &'a str,
    code: &'static str,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum HostTermination {
    Completed,
    InputLimitRejected,
    OutputLimitRejected,
    ErrorLimitRejected,
    DeadlineExpired,
    AuthenticatedCancel,
    AbnormalExit(i32),
}

/// Host-internal execution facts. This type is deliberately not deserializable:
/// neither the socket caller nor the untrusted Pack may submit acceptance facts.
#[derive(Debug)]
pub(crate) struct HostExecutionEvidence {
    pub(crate) scenario: String,
    pub(crate) nonce: String,
    pub(crate) guest_artifact_identity: String,
    pub(crate) attestation_digest: String,
    pub(crate) request_digest: String,
    pub(crate) original_deadline_ns: u64,
    pub(crate) finished_ns: u64,
    pub(crate) termination: HostTermination,
    pub(crate) authenticated_cancel_ack: bool,
    pub(crate) invocation_reaped: bool,
    pub(crate) resource_reservation_released: bool,
    pub(crate) materialization_released: bool,
}

pub(crate) type AcceptanceHandler =
    dyn Fn(AcceptanceRequest) -> Result<HostExecutionEvidence> + Send + Sync + 'static;

/// Start a private adapter socket below the isolated CI/E2E user-data root.
pub(crate) fn start(user_data_root: &Path, handler: Arc<AcceptanceHandler>) -> Result<PathBuf> {
    let root = user_data_root.join("packvm-acceptance");
    prepare_private_root(user_data_root, &root)?;
    let socket_path = root.join("adapter.sock");
    reclaim_stale_socket(&socket_path)?;
    let listener = UnixListener::bind(&socket_path)
        .context("failed to bind PackVM acceptance adapter socket")?;
    fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))
        .context("failed to restrict PackVM acceptance adapter socket")?;
    verify_private_socket(&socket_path)?;
    let thread_socket_path = socket_path.clone();
    thread::Builder::new()
        .name("packvm-acceptance".to_string())
        .spawn(move || {
            for connection in listener.incoming() {
                let response = match connection {
                    Ok(mut stream) => handle_stream(&mut stream, &handler),
                    Err(_) => break,
                };
                if response.is_err() {
                    continue;
                }
            }
            let _ = fs::remove_file(thread_socket_path);
        })
        .context("failed to start PackVM acceptance adapter")?;
    Ok(socket_path)
}

fn reclaim_stale_socket(path: &Path) -> Result<()> {
    let initial = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error).context("failed to inspect PackVM acceptance socket path"),
    };
    verify_reclaimable_socket(&initial)?;

    match UnixStream::connect(path) {
        Ok(_) => bail!("PackVM acceptance socket is already active"),
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()),
        Err(error) if error.kind() == ErrorKind::ConnectionRefused => {}
        Err(error) => {
            return Err(error).context("failed to probe existing PackVM acceptance socket")
        }
    }

    let current = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error).context("failed to recheck PackVM acceptance socket path"),
    };
    verify_reclaimable_socket(&current)?;
    if socket_identity(&initial) != socket_identity(&current) {
        bail!("PackVM acceptance socket changed during stale recovery");
    }
    fs::remove_file(path).context("failed to remove stale PackVM acceptance socket")
}

fn verify_reclaimable_socket(metadata: &fs::Metadata) -> Result<()> {
    if metadata.file_type().is_symlink()
        || !metadata.file_type().is_socket()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.nlink() != 1
        || metadata.permissions().mode() & 0o077 != 0
    {
        bail!("PackVM acceptance socket path is not a private owned socket");
    }
    Ok(())
}

fn socket_identity(metadata: &fs::Metadata) -> (u64, u64, u32, u64, u32) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.mode(),
        metadata.nlink(),
        metadata.uid(),
    )
}

fn prepare_private_root(user_data_root: &Path, root: &Path) -> Result<()> {
    let user_data = user_data_root
        .canonicalize()
        .context("PackVM acceptance user-data root is unavailable")?;
    let metadata = fs::symlink_metadata(&user_data)?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o022 != 0
    {
        bail!("PackVM acceptance requires a private owned user-data root");
    }
    fs::create_dir_all(root)?;
    fs::set_permissions(root, fs::Permissions::from_mode(0o700))?;
    let canonical = root.canonicalize()?;
    if canonical.parent() != Some(user_data.as_path()) {
        bail!("PackVM acceptance socket root escaped user-data");
    }
    let metadata = fs::symlink_metadata(&canonical)?;
    if metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0
    {
        bail!("PackVM acceptance socket root is not private");
    }
    Ok(())
}

fn verify_private_socket(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !metadata.file_type().is_socket()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0
    {
        bail!("PackVM acceptance adapter socket is not private");
    }
    Ok(())
}

fn handle_stream(stream: &mut UnixStream, handler: &Arc<AcceptanceHandler>) -> Result<()> {
    let size = read_size(stream)?;
    if size == 0 || size > MAX_REQUEST_BYTES {
        bail!("PackVM acceptance request size is invalid");
    }
    let mut body = vec![0_u8; size];
    stream.read_exact(&mut body)?;
    let request: AcceptanceRequest =
        serde_json::from_slice(&body).context("PackVM acceptance request is invalid")?;
    validate_request(&request)?;
    let scenario = request.scenario.clone();
    let nonce = request.nonce.clone();
    let response = handler(request)
        .and_then(project_host_evidence)
        .unwrap_or_else(|_| {
            serde_json::to_value(DiagnosticResponse {
                kind: "tobkiri.packvm.sandbox-acceptance.diagnostic.v1",
                scenario: &scenario,
                nonce: &nonce,
                code: "QA_PROFILE_OR_BROKER_UNAVAILABLE",
            })
            .expect("static diagnostic is serializable")
        });
    let encoded = serde_json::to_vec(&response)?;
    if encoded.is_empty() || encoded.len() > MAX_RESPONSE_BYTES {
        bail!("PackVM acceptance response size is invalid");
    }
    stream.write_all(&(encoded.len() as u32).to_be_bytes())?;
    stream.write_all(&encoded)?;
    Ok(())
}

fn project_host_evidence(evidence: HostExecutionEvidence) -> Result<serde_json::Value> {
    validate_request(&AcceptanceRequest {
        kind: REQUEST_KIND.to_string(),
        scenario: evidence.scenario.clone(),
        nonce: evidence.nonce.clone(),
    })?;
    for digest in [
        &evidence.guest_artifact_identity,
        &evidence.attestation_digest,
        &evidence.request_digest,
    ] {
        if digest.len() != 71
            || !digest.starts_with("sha256:")
            || !digest[7..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            bail!("PackVM acceptance Host evidence digest is invalid");
        }
    }
    if evidence.original_deadline_ns == 0
        || evidence.finished_ns == 0
        || !evidence.invocation_reaped
        || !evidence.resource_reservation_released
        || !evidence.materialization_released
    {
        bail!("PackVM acceptance resource cleanup is unconfirmed");
    }
    let outcome = match evidence.scenario.as_str() {
        "probe_isolation" if evidence.termination == HostTermination::Completed => "denied",
        "stdin_overflow" if evidence.termination == HostTermination::InputLimitRejected => {
            "input_limit_rejected"
        }
        "stdout_overflow" if evidence.termination == HostTermination::OutputLimitRejected => {
            "output_limit_rejected"
        }
        "stderr_overflow" if evidence.termination == HostTermination::ErrorLimitRejected => {
            "error_limit_rejected"
        }
        "original_deadline"
            if evidence.termination == HostTermination::DeadlineExpired
                && evidence.authenticated_cancel_ack
                && evidence.finished_ns >= evidence.original_deadline_ns =>
        {
            "deadline_expired"
        }
        "cancel"
            if evidence.termination == HostTermination::AuthenticatedCancel
                && evidence.authenticated_cancel_ack =>
        {
            "cancelled"
        }
        "abnormal_exit" if matches!(evidence.termination, HostTermination::AbnormalExit(code) if code != 0) => {
            "execution_failed"
        }
        "resource_cleanup" if evidence.termination == HostTermination::Completed => "released",
        _ => bail!("PackVM acceptance Host outcome is not proven"),
    };
    Ok(serde_json::json!({
        "kind": "tobkiri.packvm.sandbox-acceptance.v1",
        "scenario": evidence.scenario,
        "nonce": evidence.nonce,
        "outcome": outcome,
        "execution_boundary": "linux-packvm-guest",
        "transport": "authenticated-vsock-signed-guest-envelope",
        "guest_artifact_identity": evidence.guest_artifact_identity,
        "attestation_digest": evidence.attestation_digest,
        "request_digest": evidence.request_digest,
        "original_deadline_ns": evidence.original_deadline_ns,
        "finished_ns": evidence.finished_ns,
        "resource_cleanup_confirmed": true,
    }))
}

fn read_size(stream: &mut UnixStream) -> Result<usize> {
    let mut prefix = [0_u8; 4];
    stream.read_exact(&mut prefix)?;
    Ok(u32::from_be_bytes(prefix) as usize)
}

fn validate_request(request: &AcceptanceRequest) -> Result<()> {
    if request.kind != REQUEST_KIND
        || !SCENARIOS.contains(&request.scenario.as_str())
        || request.nonce.len() != 64
        || !request.nonce.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        bail!("PackVM acceptance request binding is invalid");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temporary_directory(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "tobkiri-packvm-acceptance-{label}-{}",
            std::process::id()
        ));
        fs::create_dir(&path).unwrap();
        path
    }

    #[test]
    fn stale_private_socket_is_reclaimed() {
        let temporary = temporary_directory("stale");
        let socket_path = temporary.join("adapter.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600)).unwrap();
        drop(listener);

        reclaim_stale_socket(&socket_path).unwrap();

        assert!(!socket_path.exists());
        fs::remove_dir(temporary).unwrap();
    }

    #[test]
    fn live_or_non_socket_paths_are_never_reclaimed() {
        let temporary = temporary_directory("live");
        let socket_path = temporary.join("adapter.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(reclaim_stale_socket(&socket_path).is_err());
        assert!(socket_path.exists());

        let file_path = temporary.join("ordinary-file");
        fs::write(&file_path, b"not a socket").unwrap();
        fs::set_permissions(&file_path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(reclaim_stale_socket(&file_path).is_err());
        assert_eq!(fs::read(&file_path).unwrap(), b"not a socket");
        drop(listener);
        fs::remove_file(socket_path).unwrap();
        fs::remove_file(file_path).unwrap();
        fs::remove_dir(temporary).unwrap();
    }

    #[test]
    fn request_is_finite_and_nonce_bound() {
        let request = AcceptanceRequest {
            kind: REQUEST_KIND.to_string(),
            scenario: "probe_isolation".to_string(),
            nonce: "a".repeat(64),
        };
        validate_request(&request).unwrap();
        for bad in ["other", "", "PROBE_ISOLATION"] {
            let request = AcceptanceRequest {
                kind: REQUEST_KIND.to_string(),
                scenario: bad.to_string(),
                nonce: "a".repeat(64),
            };
            assert!(validate_request(&request).is_err());
        }
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let value = serde_json::json!({
            "kind": REQUEST_KIND,
            "scenario": "probe_isolation",
            "nonce": "a".repeat(64),
            "approved": true,
        });
        assert!(serde_json::from_value::<AcceptanceRequest>(value).is_err());
    }

    fn evidence(scenario: &str, termination: HostTermination) -> HostExecutionEvidence {
        HostExecutionEvidence {
            scenario: scenario.to_string(),
            nonce: "a".repeat(64),
            guest_artifact_identity: format!("sha256:{}", "b".repeat(64)),
            attestation_digest: format!("sha256:{}", "c".repeat(64)),
            request_digest: format!("sha256:{}", "d".repeat(64)),
            original_deadline_ns: 100,
            finished_ns: 101,
            termination,
            authenticated_cancel_ack: matches!(
                termination,
                HostTermination::AuthenticatedCancel | HostTermination::DeadlineExpired
            ),
            invocation_reaped: true,
            resource_reservation_released: true,
            materialization_released: true,
        }
    }

    #[test]
    fn terminal_outcomes_require_typed_host_proof() {
        let cases = [
            (
                "stdin_overflow",
                HostTermination::InputLimitRejected,
                "input_limit_rejected",
            ),
            (
                "stdout_overflow",
                HostTermination::OutputLimitRejected,
                "output_limit_rejected",
            ),
            (
                "stderr_overflow",
                HostTermination::ErrorLimitRejected,
                "error_limit_rejected",
            ),
            (
                "original_deadline",
                HostTermination::DeadlineExpired,
                "deadline_expired",
            ),
            ("cancel", HostTermination::AuthenticatedCancel, "cancelled"),
            (
                "abnormal_exit",
                HostTermination::AbnormalExit(73),
                "execution_failed",
            ),
            ("resource_cleanup", HostTermination::Completed, "released"),
        ];
        for (scenario, termination, outcome) in cases {
            let projected = project_host_evidence(evidence(scenario, termination)).unwrap();
            assert_eq!(projected["outcome"], outcome);
            assert_eq!(projected["resource_cleanup_confirmed"], true);
        }
    }

    #[test]
    fn normal_errors_and_incomplete_cleanup_never_become_evidence() {
        let mut ordinary_error = evidence("cancel", HostTermination::Completed);
        ordinary_error.authenticated_cancel_ack = true;
        assert!(project_host_evidence(ordinary_error).is_err());

        let mut missing_cancel_ack = evidence("cancel", HostTermination::AuthenticatedCancel);
        missing_cancel_ack.authenticated_cancel_ack = false;
        assert!(project_host_evidence(missing_cancel_ack).is_err());

        let mut unreaped = evidence("abnormal_exit", HostTermination::AbnormalExit(73));
        unreaped.invocation_reaped = false;
        assert!(project_host_evidence(unreaped).is_err());

        let renewed_deadline = HostExecutionEvidence {
            finished_ns: 99,
            ..evidence("original_deadline", HostTermination::DeadlineExpired)
        };
        assert!(project_host_evidence(renewed_deadline).is_err());
    }
}
