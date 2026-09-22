//! Kernel health-check via HTTP.
//!
//! The Kernel exposes `GET /health` on its API port (default 8765).
//! A 200 response means the Kernel is ready to serve requests.

use std::sync::OnceLock;
use std::time::Duration;

use anyhow::{bail, Result};
use log::info;
use rand::{distributions::Alphanumeric, Rng};
use serde::Deserialize;
use sha2::{Digest, Sha256};

/// Reusable blocking HTTP client for health checks.
static HEALTH_CLIENT: OnceLock<reqwest::blocking::Client> = OnceLock::new();

#[derive(Debug, Deserialize)]
struct ApiEnvelope<T> {
    success: bool,
    data: Option<T>,
}

#[derive(Debug, Deserialize)]
struct HealthPayload {
    panel_ready: Option<bool>,
    runtime_ready: Option<bool>,
    desktop_challenge_response: Option<String>,
}

const DESKTOP_HEALTH_CHALLENGE_HEADER: &str = "X-Rumi-Desktop-Health-Challenge";

fn generate_health_challenge() -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect()
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

fn hmac_sha256_hex(secret: &str, message: &str) -> String {
    const BLOCK_SIZE: usize = 64;

    let mut key = secret.as_bytes().to_vec();
    if key.len() > BLOCK_SIZE {
        key = Sha256::digest(&key).to_vec();
    }
    key.resize(BLOCK_SIZE, 0);

    let mut outer_key_pad = [0x5c; BLOCK_SIZE];
    let mut inner_key_pad = [0x36; BLOCK_SIZE];
    for (idx, byte) in key.iter().enumerate() {
        outer_key_pad[idx] ^= byte;
        inner_key_pad[idx] ^= byte;
    }

    let mut inner = Sha256::new();
    inner.update(inner_key_pad);
    inner.update(message.as_bytes());
    let inner_hash = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(outer_key_pad);
    outer.update(inner_hash);
    hex_lower(&outer.finalize())
}

fn health_client() -> &'static reqwest::blocking::Client {
    HEALTH_CLIENT.get_or_init(|| {
        reqwest::blocking::Client::builder()
            .timeout(Duration::from_millis(800))
            .build()
            .expect("failed to build health-check HTTP client")
    })
}

fn fetch_authenticated_health(port: u16, bootstrap_secret: &str) -> Result<Option<HealthPayload>> {
    if bootstrap_secret.is_empty() {
        return Ok(None);
    }

    let challenge = generate_health_challenge();
    let url = format!("http://127.0.0.1:{port}/health");
    let resp = match health_client()
        .get(&url)
        .header(DESKTOP_HEALTH_CHALLENGE_HEADER, &challenge)
        .send()
    {
        Ok(resp) => resp,
        Err(_) => return Ok(None),
    };

    if !resp.status().is_success() {
        return Ok(None);
    }

    let envelope: ApiEnvelope<HealthPayload> = match resp.json() {
        Ok(payload) => payload,
        Err(_) => return Ok(None),
    };
    if !envelope.success {
        return Ok(None);
    }

    let Some(payload) = envelope.data else {
        return Ok(None);
    };
    if payload.panel_ready == Some(false) {
        return Ok(None);
    }

    let expected = hmac_sha256_hex(bootstrap_secret, &challenge);
    if !payload
        .desktop_challenge_response
        .as_deref()
        .is_some_and(|actual| actual.eq_ignore_ascii_case(&expected))
    {
        return Ok(None);
    }
    Ok(Some(payload))
}

/// Send a health-check request that proves the listener knows the desktop
/// bootstrap secret without disclosing that secret to an untrusted local port.
pub fn check_authenticated_health(port: u16, bootstrap_secret: &str) -> Result<bool> {
    Ok(fetch_authenticated_health(port, bootstrap_secret)?.is_some())
}

/// Return whether an authenticated Kernel has completed runtime activation.
pub fn check_authenticated_runtime_ready(port: u16, bootstrap_secret: &str) -> Result<bool> {
    Ok(fetch_authenticated_health(port, bootstrap_secret)?
        .and_then(|payload| payload.runtime_ready)
        .unwrap_or(false))
}

/// Send a single health-check request.
///
/// Returns `Ok(true)` if the Kernel responded with HTTP 200,
/// `Ok(false)` for any other status or a connection error.
pub fn check_health(port: u16) -> Result<bool> {
    let url = format!("http://127.0.0.1:{port}/health");

    match health_client().get(&url).send() {
        Ok(resp) => {
            if !resp.status().is_success() {
                return Ok(false);
            }

            let envelope: ApiEnvelope<HealthPayload> = match resp.json() {
                Ok(payload) => payload,
                Err(_) => return Ok(true),
            };

            if !envelope.success {
                return Ok(false);
            }

            Ok(envelope
                .data
                .and_then(|payload| payload.panel_ready)
                .unwrap_or(true))
        }
        Err(_) => Ok(false),
    }
}

/// Poll `GET /health` until the Kernel is ready or `timeout_secs` elapses.
///
/// Checks every 200 ms.
pub fn wait_for_healthy(port: u16, timeout_secs: u64) -> Result<()> {
    info!("Waiting for Kernel health-check on port {port} (timeout {timeout_secs}s) ...");

    let start = std::time::Instant::now();
    let timeout = Duration::from_secs(timeout_secs);
    let interval = Duration::from_millis(200);

    while start.elapsed() < timeout {
        if check_health(port)? {
            info!("Kernel healthy after ~{:?}", start.elapsed());
            return Ok(());
        }
        std::thread::sleep(interval);
    }

    bail!("Kernel did not become healthy within {timeout_secs}s on port {port}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn check_health_unreachable_port() {
        let result = check_health(1);
        assert!(result.is_ok());
        assert!(!result.unwrap());
    }

    #[test]
    fn hmac_sha256_hex_matches_known_vector() {
        assert_eq!(
            hmac_sha256_hex("key", "The quick brown fox jumps over the lazy dog"),
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
        );
    }
}
