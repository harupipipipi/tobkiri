//! Application update checker — V1 implementation.
//!
//! Checks the GitHub Releases API for a newer version and, if found,
//! offers to open the release page in the user's browser.

use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

/// Information about an available update.
#[derive(Debug, Clone)]
pub struct UpdateInfo {
    /// The latest version string, e.g. "0.2.0".
    pub latest_version: String,
    /// URL to the GitHub release page.
    pub release_url: String,
    /// The currently running version, e.g. "0.1.0".
    pub current_version: String,
}

/// Stable Launcher-facing result for an explicit update check.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LauncherUpdateStatus {
    /// `available` when a newer release exists, otherwise `up_to_date`.
    pub state: &'static str,
    /// The currently running Launcher version.
    pub current_version: String,
    /// The newer release version, when one exists.
    pub latest_version: Option<String>,
}

/// Partial GitHub Releases API response.
#[derive(Debug, Deserialize)]
struct GitHubRelease {
    tag_name: String,
    html_url: String,
}

/// The only repository from which Launcher release updates may be read.
pub const RELEASE_REPOSITORY: &str = "harupipipipi/tobkiri";

/// HTTP request timeout in seconds.
const TIMEOUT_SECS: u64 = 10;

/// Check whether a newer version is available on GitHub Releases.
///
/// Returns `Ok(Some(UpdateInfo))` if an update exists, `Ok(None)` if the
/// current version is up-to-date, or an error on network / parse failure.
///
/// Errors are **not** fatal — callers should log and continue.
pub fn check_for_update() -> Result<Option<UpdateInfo>> {
    let current_str = env!("CARGO_PKG_VERSION");
    let current = parse_version(current_str).context("failed to parse current version")?;

    let release = fetch_latest_release().context("failed to fetch latest release")?;

    let latest = parse_version(&release.tag_name)
        .with_context(|| format!("failed to parse release tag: {}", release.tag_name))?;

    if latest > current {
        Ok(Some(UpdateInfo {
            latest_version: latest.to_string(),
            release_url: release.html_url,
            current_version: current.to_string(),
        }))
    } else {
        Ok(None)
    }
}

/// Check the official Launcher release channel and return a UI-safe status.
pub fn check_for_update_status() -> Result<LauncherUpdateStatus> {
    let current_version = parse_version(env!("CARGO_PKG_VERSION"))
        .context("failed to parse current version")?
        .to_string();
    match check_for_update()? {
        Some(info) => Ok(LauncherUpdateStatus {
            state: "available",
            current_version: info.current_version,
            latest_version: Some(info.latest_version),
        }),
        None => Ok(LauncherUpdateStatus {
            state: "up_to_date",
            current_version,
            latest_version: None,
        }),
    }
}

/// Open the release page in the user's default browser.
pub fn open_release_page(info: &UpdateInfo) -> Result<()> {
    validate_release_url(&info.release_url)?;
    open::that_detached(&info.release_url).context("failed to open release page in browser")?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/// Fetch the latest release metadata from the GitHub API.
fn fetch_latest_release() -> Result<GitHubRelease> {
    let version = env!("CARGO_PKG_VERSION");
    let releases_api = format!("https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest");
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(TIMEOUT_SECS))
        .user_agent(format!("tobkiri-launcher/{version}"))
        .build()
        .context("failed to build HTTP client")?;

    let resp = client
        .get(releases_api)
        .header("Accept", "application/vnd.github+json")
        .send()
        .context("GitHub API request failed")?;

    if !resp.status().is_success() {
        bail!("GitHub API returned HTTP {}", resp.status());
    }

    let release: GitHubRelease = resp.json().context("failed to parse GitHub release JSON")?;

    Ok(release)
}

/// Parse a version string, stripping an optional leading `v`.
fn parse_version(tag: &str) -> Result<semver::Version> {
    let cleaned = tag.strip_prefix('v').unwrap_or(tag);
    semver::Version::parse(cleaned).with_context(|| format!("invalid semver: {tag}"))
}

fn validate_release_url(release_url: &str) -> Result<()> {
    let url = reqwest::Url::parse(release_url).context("release URL is invalid")?;
    let expected_path = format!("/{RELEASE_REPOSITORY}/releases/");
    if url.scheme() != "https"
        || url.host_str() != Some("github.com")
        || !url.path().starts_with(&expected_path)
    {
        bail!("release URL is outside the official Tobkiri release channel");
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_version_with_v_prefix() {
        let v = parse_version("v1.2.3").unwrap();
        assert_eq!(v, semver::Version::new(1, 2, 3));
    }

    #[test]
    fn parse_version_without_prefix() {
        let v = parse_version("1.2.3").unwrap();
        assert_eq!(v, semver::Version::new(1, 2, 3));
    }

    #[test]
    fn parse_version_invalid() {
        assert!(parse_version("invalid").is_err());
    }

    #[test]
    fn update_detected_when_latest_is_newer() {
        let current = parse_version("0.1.0").unwrap();
        let latest = parse_version("0.2.0").unwrap();
        assert!(latest > current);
    }

    #[test]
    fn no_update_when_current_is_latest() {
        let current = parse_version("1.0.0").unwrap();
        let latest = parse_version("1.0.0").unwrap();
        assert!(latest <= current);
    }

    #[test]
    fn release_origin_is_bound_to_the_tobkiri_repository() {
        assert_eq!(RELEASE_REPOSITORY, "harupipipipi/tobkiri");
        assert_eq!(
            format!("https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"),
            "https://api.github.com/repos/harupipipipi/tobkiri/releases/latest"
        );
        assert!(!RELEASE_REPOSITORY.contains("rumiai"));
    }

    #[test]
    fn release_page_is_bound_to_the_official_https_release_path() {
        assert!(validate_release_url(
            "https://github.com/harupipipipi/tobkiri/releases/tag/v1.2.3"
        )
        .is_ok());
        assert!(
            validate_release_url("http://github.com/harupipipipi/tobkiri/releases/tag/v1.2.3")
                .is_err()
        );
        assert!(
            validate_release_url("https://github.com/harupipipipi/other/releases/tag/v1.2.3")
                .is_err()
        );
        assert!(validate_release_url(
            "https://github.com.evil.example/harupipipipi/tobkiri/releases/tag/v1.2.3"
        )
        .is_err());
    }

    #[test]
    fn launcher_update_status_serializes_with_a_stable_frontend_shape() {
        let status = LauncherUpdateStatus {
            state: "available",
            current_version: "1.0.0".into(),
            latest_version: Some("1.1.0".into()),
        };
        assert_eq!(
            serde_json::to_value(status).unwrap(),
            serde_json::json!({
                "state": "available",
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
            })
        );
    }
}
