//! Non-publishable native bridge for explicit PackVM sandbox acceptance.
//!
//! The socket is a control channel only.  It never manufactures guest evidence:
//! the supplied handler must use the normal authenticated Defaultspack contract
//! route, whose Broker owns Authority, admission, PackVM attestation, deadline,
//! cancellation, and cleanup.

#![cfg(unix)]

use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

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

pub(crate) type AcceptanceHandler =
    dyn Fn(AcceptanceRequest) -> Result<Value> + Send + Sync + 'static;

/// Start a private adapter socket below the isolated CI/E2E user-data root.
pub(crate) fn start(user_data_root: &Path, handler: Arc<AcceptanceHandler>) -> Result<PathBuf> {
    let root = user_data_root.join("packvm-acceptance");
    prepare_private_root(user_data_root, &root)?;
    let socket_path = root.join("adapter.sock");
    if fs::symlink_metadata(&socket_path).is_ok() {
        bail!("PackVM acceptance socket path already exists");
    }
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
    let response = handler(request).unwrap_or_else(|_| {
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
}
