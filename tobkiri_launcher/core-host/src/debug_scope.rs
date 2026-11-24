//! Bounded trusted native debug startup scope.
//!
//! A debug build may be launched against a privately staged data root under a
//! distinct single-instance identifier with one allowed Profile, so native
//! acceptance runs never touch the normal user Core/Launcher state. The scope
//! is parsed only from explicit process inputs before the Tauri app is built;
//! it grants the frontend no new IPC, approval, Gate, or data-root authority
//! and its values are never serialized or logged toward the renderer.

use std::{
    ffi::OsStr,
    path::{Component, Path, PathBuf},
};

use tobkiri_core::ids::ProfileId;

/// Environment names consulted only by the native host entry point. A
/// partial or malformed set is a hard failure, never a fallback.
pub const DEBUG_ROOT_ENV: &str = "TOBKIRI_CORE_DEBUG_ROOT";
pub const DEBUG_NONCE_ENV: &str = "TOBKIRI_CORE_DEBUG_NONCE";
pub const DEBUG_PROFILE_ENV: &str = "TOBKIRI_CORE_DEBUG_PROFILE";

const MAX_ROOT_BYTES: usize = 4096;
const MIN_NONCE_BYTES: usize = 8;
const MAX_NONCE_BYTES: usize = 32;
const MAX_PROFILE_BYTES: usize = 256;

/// Fixed failure for a malformed debug scope. The offending value is never
/// echoed into an error, log, or frontend payload.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DebugScopeError;

impl std::fmt::Display for DebugScopeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("invalid Tobkiri Core debug scope")
    }
}

impl std::error::Error for DebugScopeError {}

/// An explicitly requested isolated debug environment for the native host.
#[derive(Clone, Debug)]
pub struct DebugScope {
    root: PathBuf,
    identifier: String,
    title: String,
    profile: ProfileId,
}

impl DebugScope {
    /// Parse a scope from explicit values so callers and tests never mutate
    /// the process environment. `Ok(None)` requires every input to be
    /// absent; any partial, non-unicode, oversized, or ambiguous input fails
    /// closed rather than silently falling back to normal user data.
    pub fn parse(
        root: Option<&OsStr>,
        nonce: Option<&OsStr>,
        profile: Option<&OsStr>,
    ) -> Result<Option<Self>, DebugScopeError> {
        if root.is_none() && nonce.is_none() && profile.is_none() {
            return Ok(None);
        }
        let (Some(root), Some(nonce), Some(profile)) = (root, nonce, profile) else {
            return Err(DebugScopeError);
        };
        let root = root.to_str().ok_or(DebugScopeError)?;
        if root.is_empty() || root.len() > MAX_ROOT_BYTES {
            return Err(DebugScopeError);
        }
        let root = PathBuf::from(root);
        if !root.is_absolute()
            || !root
                .components()
                .all(|component| matches!(component, Component::RootDir | Component::Normal(_)))
        {
            return Err(DebugScopeError);
        }
        let nonce = nonce.to_str().ok_or(DebugScopeError)?;
        if !(MIN_NONCE_BYTES..=MAX_NONCE_BYTES).contains(&nonce.len())
            || !nonce
                .bytes()
                .all(|byte| matches!(byte, b'a'..=b'z' | b'0'..=b'9'))
        {
            return Err(DebugScopeError);
        }
        let profile = profile.to_str().ok_or(DebugScopeError)?;
        if profile.is_empty() || profile.len() > MAX_PROFILE_BYTES {
            return Err(DebugScopeError);
        }
        let profile = ProfileId::parse(profile).map_err(|_| DebugScopeError)?;
        Ok(Some(Self {
            root,
            identifier: format!("dev.tobkiri.native-debug.{nonce}"),
            title: format!("Tobkiri Harness [{nonce}]"),
            profile,
        }))
    }

    /// Read the scope from this process's environment at native startup.
    /// Debug builds accept one complete scope; a release build fails closed
    /// when any variable is present so it can never open normal user data
    /// under an ignored debug request.
    #[cfg(debug_assertions)]
    pub fn from_env() -> Result<Option<Self>, DebugScopeError> {
        Self::parse(
            std::env::var_os(DEBUG_ROOT_ENV).as_deref(),
            std::env::var_os(DEBUG_NONCE_ENV).as_deref(),
            std::env::var_os(DEBUG_PROFILE_ENV).as_deref(),
        )
    }

    #[cfg(not(debug_assertions))]
    pub fn from_env() -> Result<Option<Self>, DebugScopeError> {
        if [DEBUG_ROOT_ENV, DEBUG_NONCE_ENV, DEBUG_PROFILE_ENV]
            .iter()
            .any(|name| std::env::var_os(name).is_some())
        {
            return Err(DebugScopeError);
        }
        Ok(None)
    }

    /// The private staged data root `Core::open` uses instead of the
    /// platform default.
    pub fn data_root(&self) -> &Path {
        &self.root
    }

    /// The distinct bundle identifier isolating the native single-instance
    /// socket from the real Launcher's identity.
    pub fn identifier(&self) -> &str {
        &self.identifier
    }

    /// The unique window title for the `core-host` window while scoped.
    pub fn window_title(&self) -> &str {
        &self.title
    }

    /// The single Profile this scope allows `core_start_profile` to run.
    pub fn allowed_profile(&self) -> &ProfileId {
        &self.profile
    }

    /// Exact allowlist check performed before any startup or transition.
    pub fn allows_profile(&self, profile: &ProfileId) -> bool {
        self.profile.eq(profile)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn staged_root() -> (tempfile::TempDir, PathBuf) {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().canonicalize().unwrap();
        (directory, root)
    }

    #[test]
    fn absent_environment_requests_no_scope() {
        assert!(matches!(DebugScope::parse(None, None, None), Ok(None)));
    }

    #[test]
    fn a_complete_scope_derives_an_isolated_native_identity() {
        let (_directory, root) = staged_root();
        let scope = DebugScope::parse(
            Some(root.as_os_str()),
            Some(OsStr::new("abc12345")),
            Some(OsStr::new("prof_debug_scope")),
        )
        .unwrap()
        .unwrap();
        assert_eq!(scope.data_root(), root.as_path());
        assert_eq!(scope.identifier(), "dev.tobkiri.native-debug.abc12345");
        assert_eq!(scope.window_title(), "Tobkiri Harness [abc12345]");
        let allowed = ProfileId::parse("prof_debug_scope").unwrap();
        assert!(scope.allows_profile(&allowed));
        assert_eq!(scope.allowed_profile(), &allowed);
    }

    #[test]
    fn each_nonce_scopes_a_distinct_identifier_and_title() {
        let (_directory, root) = staged_root();
        let profile = OsStr::new("prof_debug_scope");
        let first = DebugScope::parse(
            Some(root.as_os_str()),
            Some(OsStr::new("aaaaaaaa")),
            Some(profile),
        )
        .unwrap()
        .unwrap();
        let second = DebugScope::parse(
            Some(root.as_os_str()),
            Some(OsStr::new("bbbbbbbb")),
            Some(profile),
        )
        .unwrap()
        .unwrap();
        assert_ne!(first.identifier(), second.identifier());
        assert_ne!(first.window_title(), second.window_title());
        assert_eq!(first.allowed_profile(), second.allowed_profile());
    }

    #[test]
    fn partial_or_missing_siblings_fail_closed() {
        let (_directory, root) = staged_root();
        let root = root.as_os_str();
        let nonce = OsStr::new("abc12345");
        let profile = OsStr::new("prof_debug_scope");
        for inputs in [
            (Some(root), None, None),
            (None, Some(nonce), None),
            (None, None, Some(profile)),
            (Some(root), Some(nonce), None),
            (Some(root), None, Some(profile)),
            (None, Some(nonce), Some(profile)),
        ] {
            assert!(DebugScope::parse(inputs.0, inputs.1, inputs.2).is_err());
        }
    }

    #[test]
    fn non_absolute_or_ambiguous_roots_fail_closed() {
        let nonce = OsStr::new("abc12345");
        let profile = OsStr::new("prof_debug_scope");
        for root in [
            "",
            "relative",
            "nested/relative",
            "../escape",
            "/tmp/../escape",
            "/private/tmp/../escape",
        ] {
            assert!(DebugScope::parse(Some(OsStr::new(root)), Some(nonce), Some(profile)).is_err());
        }
    }

    #[test]
    fn oversized_roots_fail_closed() {
        let nonce = OsStr::new("abc12345");
        let profile = OsStr::new("prof_debug_scope");
        let oversized = format!("/{}", "a".repeat(MAX_ROOT_BYTES));
        assert!(
            DebugScope::parse(Some(OsStr::new(&oversized)), Some(nonce), Some(profile)).is_err()
        );
    }

    #[test]
    fn nonce_charset_and_length_bounds_are_enforced() {
        let (_directory, root) = staged_root();
        let root = root.as_os_str();
        let profile = OsStr::new("prof_debug_scope");
        for nonce in [
            "abc1234",
            "ABC12345",
            "abc-1234",
            "abc_1234",
            "abc 1234",
            "abcdefgh!",
        ] {
            assert!(DebugScope::parse(Some(root), Some(OsStr::new(nonce)), Some(profile)).is_err());
        }
        let oversized = "a".repeat(MAX_NONCE_BYTES + 1);
        assert!(
            DebugScope::parse(Some(root), Some(OsStr::new(&oversized)), Some(profile)).is_err()
        );
        for nonce in ["abcdefgh", "00000000"] {
            assert!(
                DebugScope::parse(Some(root), Some(OsStr::new(nonce)), Some(profile))
                    .unwrap()
                    .is_some()
            );
        }
        let maximum = "z9".repeat(16);
        assert!(
            DebugScope::parse(Some(root), Some(OsStr::new(&maximum)), Some(profile))
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn the_profile_must_parse_and_match_exactly() {
        let (_directory, root) = staged_root();
        let root = root.as_os_str();
        let nonce = OsStr::new("abc12345");
        for profile in ["", "../profile", "with space", "prof/scope"] {
            assert!(DebugScope::parse(Some(root), Some(nonce), Some(OsStr::new(profile))).is_err());
        }
        let oversized = "p".repeat(MAX_PROFILE_BYTES + 1);
        assert!(DebugScope::parse(Some(root), Some(nonce), Some(OsStr::new(&oversized))).is_err());
        let scope = DebugScope::parse(
            Some(root),
            Some(nonce),
            Some(OsStr::new("prof_debug_scope")),
        )
        .unwrap()
        .unwrap();
        assert!(!scope.allows_profile(&ProfileId::parse("prof_other").unwrap()));
        assert!(!scope.allows_profile(&ProfileId::parse("prof_debug_scope_extra").unwrap()));
    }

    #[cfg(unix)]
    #[test]
    fn non_unicode_inputs_fail_closed() {
        use std::os::unix::ffi::OsStrExt;
        let (_directory, root) = staged_root();
        let bad = OsStr::from_bytes(&[0x66, 0x6f, 0x80]);
        assert!(
            DebugScope::parse(
                Some(bad),
                Some(OsStr::new("abc12345")),
                Some(OsStr::new("prof_debug_scope"))
            )
            .is_err()
        );
        assert!(
            DebugScope::parse(
                Some(root.as_os_str()),
                Some(bad),
                Some(OsStr::new("prof_debug_scope"))
            )
            .is_err()
        );
        assert!(
            DebugScope::parse(
                Some(root.as_os_str()),
                Some(OsStr::new("abc12345")),
                Some(bad)
            )
            .is_err()
        );
    }
}
