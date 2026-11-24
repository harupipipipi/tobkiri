//! Platform-independent preflight for the native acceptance socket pathname.

use std::path::Path;

use anyhow::{bail, Context, Result};

/// The Darwin `sockaddr_un.sun_path` buffer has 104 bytes, including NUL.
pub(crate) const MACOS_UNIX_SOCKET_PATH_CAPACITY: usize = 104;

pub(crate) fn validate_socket_path_for_limit(path: &Path, capacity: usize) -> Result<()> {
    let pathname = path
        .to_str()
        .context("PackVM acceptance socket path must be UTF-8")?;
    let bytes = pathname.as_bytes();
    if bytes.contains(&0) || bytes.len() >= capacity {
        bail!(
            "PackVM acceptance socket path is {} bytes; AF_UNIX allows at most {} bytes. Use a shorter private CI/E2E app-data root",
            bytes.len(),
            capacity.saturating_sub(1),
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn macos_acceptance_path_preflight_rejects_long_ci_root() {
        let long_root = "/Users/runner/work/_temp/tobkiri-launcher-cold-boot-data-aarch64-apple-darwin-00000000000000000000000000000000/ci-e2e-app-data";
        let socket = Path::new(long_root).join("user_data/packvm-acceptance/adapter.sock");
        let error = validate_socket_path_for_limit(&socket, MACOS_UNIX_SOCKET_PATH_CAPACITY)
            .expect_err("long CI root must be rejected before bind");
        assert!(error
            .to_string()
            .contains("shorter private CI/E2E app-data root"));
    }

    #[test]
    fn macos_acceptance_path_preflight_accepts_short_private_root() {
        let socket = Path::new("/private/tmp/tbk.12345678/ci-e2e-app-data")
            .join("user_data/packvm-acceptance/adapter.sock");
        validate_socket_path_for_limit(&socket, MACOS_UNIX_SOCKET_PATH_CAPACITY).unwrap();
    }

    #[test]
    fn socket_path_limit_counts_utf8_bytes_and_terminal_nul() {
        let below_limit = Path::new(&"a".repeat(103)).to_path_buf();
        let at_limit = Path::new(&"a".repeat(104)).to_path_buf();
        validate_socket_path_for_limit(&below_limit, MACOS_UNIX_SOCKET_PATH_CAPACITY).unwrap();
        assert!(
            validate_socket_path_for_limit(&at_limit, MACOS_UNIX_SOCKET_PATH_CAPACITY).is_err()
        );
        assert!(validate_socket_path_for_limit(Path::new("é"), 2).is_err());
    }
}
