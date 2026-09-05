//! Canonical cross-language wire contract for the sealed Python bootstrap.
//!
//! This module intentionally has no crate dependencies so `build.rs` and the
//! runtime use the same role identifiers, option names, and template audit.

use std::ffi::{OsStr, OsString};

pub const PROTOCOL_SCHEMA: &str = "io.tobkiri.sealed-python-launch.v3";
pub const ATTESTATION_SCHEMA: &str = "io.tobkiri.sealed-python-attestation.v2";
pub const ATTESTATION_FILE_SCHEMA: &str = "io.tobkiri.sealed-python-attestation-file.v1";
pub const BOOTSTRAP_MODULE: &str = "tobkiri_sealed.bootstrap";
pub const ROLE_TYPED: &str = "typed";
pub const ROLE_DEFAULTSPACK: &str = "defaultspack";
pub const ROLE_HOST_HELPER: &str = "host_helper";
pub const ARG_ROLE: &str = "--role";
pub const ARG_NONCE: &str = "--nonce";
pub const ARG_ATTESTATION: &str = "--attestation";
pub const ARG_MANIFEST: &str = "--manifest";
pub const ARG_ENVIRONMENT_ROOT: &str = "--environment-root";
pub const ARG_RUNTIME_OVERLAY_SHA256: &str = "--runtime-overlay-sha256";
pub const ARG_OUTER_RUNTIME_MANIFEST_SHA256: &str = "--outer-runtime-manifest-sha256";
pub const ARG_APPLICATION_BUNDLE_ROOT: &str = "--application-bundle-root";
pub const ARG_PACKVM_PROVISIONING_SHA256: &str = "--packvm-provisioning-sha256";
pub const ARG_PACKVM_HELPER_MANIFEST_SHA256: &str = "--packvm-helper-manifest-sha256";
pub const ARG_PACKVM_HELPER_TEAM_ID: &str = "--packvm-helper-team-id";
pub const ARG_SEPARATOR: &str = "--";

pub const REQUIRED_TEMPLATE_FRAGMENTS: &[&str] = &[
    PROTOCOL_SCHEMA,
    ATTESTATION_SCHEMA,
    ATTESTATION_FILE_SCHEMA,
    ROLE_TYPED,
    ROLE_DEFAULTSPACK,
    ROLE_HOST_HELPER,
    ARG_NONCE,
    ARG_ATTESTATION,
    ARG_MANIFEST,
    ARG_ENVIRONMENT_ROOT,
    ARG_RUNTIME_OVERLAY_SHA256,
    ARG_OUTER_RUNTIME_MANIFEST_SHA256,
    ARG_APPLICATION_BUNDLE_ROOT,
    ARG_PACKVM_PROVISIONING_SHA256,
    ARG_PACKVM_HELPER_MANIFEST_SHA256,
    ARG_PACKVM_HELPER_TEAM_ID,
    "O_EXCL",
    "os.link",
    "st_nlink",
    "fsync",
];

/// The sole accepted argument ordering for the bootstrap v3 boundary.
pub fn launch_arguments(
    role: &str,
    nonce: &str,
    attestation: &OsStr,
    manifest: &OsStr,
    environment_root: &OsStr,
    runtime_overlay_sha256: &str,
    outer_runtime_manifest_sha256: &str,
    application_bundle_root: &OsStr,
    packvm_provisioning_sha256: &str,
    packvm_helper_manifest_sha256: &str,
    packvm_helper_team_id: &str,
) -> Vec<OsString> {
    [
        OsString::from("-m"),
        OsString::from(BOOTSTRAP_MODULE),
        OsString::from(ARG_ROLE),
        OsString::from(role),
        OsString::from(ARG_NONCE),
        OsString::from(nonce),
        OsString::from(ARG_ATTESTATION),
        attestation.to_os_string(),
        OsString::from(ARG_MANIFEST),
        manifest.to_os_string(),
        OsString::from(ARG_ENVIRONMENT_ROOT),
        environment_root.to_os_string(),
        OsString::from(ARG_RUNTIME_OVERLAY_SHA256),
        OsString::from(runtime_overlay_sha256),
        OsString::from(ARG_OUTER_RUNTIME_MANIFEST_SHA256),
        OsString::from(outer_runtime_manifest_sha256),
        OsString::from(ARG_APPLICATION_BUNDLE_ROOT),
        application_bundle_root.to_os_string(),
        OsString::from(ARG_PACKVM_PROVISIONING_SHA256),
        OsString::from(packvm_provisioning_sha256),
        OsString::from(ARG_PACKVM_HELPER_MANIFEST_SHA256),
        OsString::from(packvm_helper_manifest_sha256),
        OsString::from(ARG_PACKVM_HELPER_TEAM_ID),
        OsString::from(packvm_helper_team_id),
    ]
    .into()
}

/// Reject a packaging bootstrap that does not implement the complete v3 wire.
pub fn validate_bootstrap_template(template: &str) -> Result<(), String> {
    let missing = REQUIRED_TEMPLATE_FRAGMENTS
        .iter()
        .copied()
        .filter(|fragment| !template.contains(fragment))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return Err(format!(
            "sealed bootstrap does not implement {PROTOCOL_SCHEMA}; missing {}",
            missing.join(", ")
        ));
    }
    if !template.contains("parse_known_args") && !template.contains("role_args") {
        return Err(format!(
            "sealed bootstrap does not preserve role arguments after {ARG_SEPARATOR}"
        ));
    }
    if template.contains("secrets.token_hex") {
        return Err(
            "sealed bootstrap generates its own nonce instead of echoing Launcher nonce".into(),
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repository_bootstrap_is_audited_against_the_canonical_wire() {
        let template = include_str!(
            "../../../.github/scripts/sealed_python_sources/tobkiri_sealed/bootstrap.py"
        );
        let result = validate_bootstrap_template(template);
        if template.contains(PROTOCOL_SCHEMA) {
            result.expect("template declaring launch v2 must implement its complete wire");
        } else {
            let error = result.expect_err("legacy template must be rejected before packaging");
            assert!(error.contains(PROTOCOL_SCHEMA));
            assert!(error.contains(ARG_NONCE));
        }
    }

    #[test]
    fn launch_wire_has_one_typed_order_before_role_separator() {
        let arguments = launch_arguments(
            "defaultspack",
            "nonce",
            OsStr::new("attest"),
            OsStr::new("manifest"),
            OsStr::new("root"),
            "overlay-digest",
            "outer-digest",
            OsStr::new("/Applications/Tobkiri Launcher.app"),
            "provisioning-digest",
            "helper-manifest-digest",
            "ABC1234567",
        );
        let strings = arguments
            .iter()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert_eq!(
            strings,
            [
                "-m",
                BOOTSTRAP_MODULE,
                ARG_ROLE,
                ROLE_DEFAULTSPACK,
                ARG_NONCE,
                "nonce",
                ARG_ATTESTATION,
                "attest",
                ARG_MANIFEST,
                "manifest",
                ARG_ENVIRONMENT_ROOT,
                "root",
                ARG_RUNTIME_OVERLAY_SHA256,
                "overlay-digest",
                ARG_OUTER_RUNTIME_MANIFEST_SHA256,
                "outer-digest",
                ARG_APPLICATION_BUNDLE_ROOT,
                "/Applications/Tobkiri Launcher.app",
                ARG_PACKVM_PROVISIONING_SHA256,
                "provisioning-digest",
                ARG_PACKVM_HELPER_MANIFEST_SHA256,
                "helper-manifest-digest",
                ARG_PACKVM_HELPER_TEAM_ID,
                "ABC1234567"
            ]
        );
    }
}
