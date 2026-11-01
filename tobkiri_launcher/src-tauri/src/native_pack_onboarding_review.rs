//! Exact signed-Pack review facts; this module grants no trust or authority.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const KEY_VERSION_SCHEMA: &str = "io.tobkiri.native-pack-key-version.v1";
const INVALID_REVIEW: &str = "Signed Pack key-version review is invalid or expired";

#[derive(Debug, Deserialize)]
pub(crate) struct NativePackOnboardingPreview {
    pack_id: String,
    version: String,
    publisher_id: String,
    key_id: String,
    key_fingerprint: String,
    requested_capabilities: Vec<String>,
    contract_versions: BTreeMap<String, String>,
    artifact_digest: String,
    preview_digest: String,
    #[serde(default)]
    predecessor_artifact_digest: Option<String>,
    #[serde(default)]
    key_version_authorization: Option<KeyVersionAuthorization>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct KeyVersionAuthorization {
    schema: String,
    publisher_id: String,
    pack_id: String,
    version: String,
    artifact_digest: String,
    signed_manifest_digest: String,
    previous_key_fingerprint: String,
    new_key_fingerprint: String,
    new_key_id: String,
    predecessor_artifact_digest: String,
    compatibility_digest: String,
    policy_digest: String,
    publisher_keyset_digest: String,
    host_owner_id: String,
    profile_revision: String,
    plan_digest: String,
    activation_id: String,
    challenge: String,
    authorization_digest: String,
    policy_generation: u64,
    security_epoch: u64,
    fencing_token: u64,
    issued_at_ms: u64,
    expires_at_ms: u64,
}

fn valid_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_digest(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .is_some_and(|hex| valid_hex(hex, 64))
}

fn visible_identity(value: &str) -> bool {
    !value.is_empty() && value.len() <= 1024 && !value.chars().any(char::is_control)
}

impl KeyVersionAuthorization {
    fn digest(&self) -> Result<String, String> {
        let value = serde_json::to_value(self).map_err(|_| INVALID_REVIEW.to_string())?;
        let mut fields: BTreeMap<String, Value> =
            serde_json::from_value(value).map_err(|_| INVALID_REVIEW.to_string())?;
        fields.remove("authorization_digest");
        let bytes = serde_json::to_vec(&fields).map_err(|_| INVALID_REVIEW.to_string())?;
        Ok(format!("sha256:{:x}", Sha256::digest(bytes)))
    }

    fn validate(&self, preview: &NativePackOnboardingPreview, now_ms: u64) -> Result<(), String> {
        let valid = self.schema == KEY_VERSION_SCHEMA
            && self.publisher_id == preview.publisher_id
            && self.pack_id == preview.pack_id
            && self.version == preview.version
            && self.artifact_digest == preview.artifact_digest
            && self.new_key_fingerprint == preview.key_fingerprint
            && self.new_key_id == preview.key_id
            && preview.predecessor_artifact_digest.as_deref()
                == Some(self.predecessor_artifact_digest.as_str())
            && [
                &self.publisher_id,
                &self.pack_id,
                &self.version,
                &self.activation_id,
            ]
            .iter()
            .all(|value| visible_identity(value))
            && [
                &self.artifact_digest,
                &self.signed_manifest_digest,
                &self.previous_key_fingerprint,
                &self.new_key_fingerprint,
                &self.predecessor_artifact_digest,
                &self.compatibility_digest,
                &self.policy_digest,
                &self.publisher_keyset_digest,
                &self.host_owner_id,
                &self.profile_revision,
                &self.plan_digest,
                &self.authorization_digest,
                &preview.preview_digest,
            ]
            .iter()
            .all(|value| valid_digest(value))
            && valid_hex(&self.new_key_id, 32)
            && (16..=128).contains(&self.challenge.len())
            && self
                .challenge
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
            && self.security_epoch > 0
            && self.fencing_token > 0
            && self
                .expires_at_ms
                .checked_sub(self.issued_at_ms)
                .is_some_and(|horizon| (1..=300_000).contains(&horizon))
            && self.issued_at_ms <= now_ms
            && now_ms < self.expires_at_ms;
        if !valid || self.digest()? != self.authorization_digest {
            return Err(INVALID_REVIEW.into());
        }
        Ok(())
    }
}

/// Return current Unix milliseconds without allowing an invalid local clock.
pub(crate) fn unix_time_ms() -> Result<u64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| INVALID_REVIEW.to_string())?;
    u64::try_from(elapsed.as_millis()).map_err(|_| INVALID_REVIEW.to_string())
}

/// Render the exact Host preview before the mandatory parented Native choice.
pub(crate) fn review_message(
    preview: &NativePackOnboardingPreview,
    now_ms: u64,
) -> Result<String, String> {
    let capabilities = if preview.requested_capabilities.is_empty() {
        "none".to_string()
    } else {
        preview.requested_capabilities.join(", ")
    };
    let contracts = if preview.contract_versions.is_empty() {
        "none".to_string()
    } else {
        preview
            .contract_versions
            .iter()
            .map(|(name, version)| format!("{name}: {version}"))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let update_review = preview.predecessor_artifact_digest.as_ref().map(|digest| {
        format!("\nRetained predecessor: {digest}\nThis adds a new revision. Existing Profiles keep their selected artifact until you review and activate an update.\n")
    }).unwrap_or_default();
    let mut message = format!(
        "Trust this separately selected publisher key for this signed Pack and add it?\n\nPack: {} {}\nPublisher: {}\nKey fingerprint: {}\nCapabilities: {}\nContracts: {}\nArtifact: {}{}\n\nThis grants trust for this exact Pack identity and artifact. Installation and capability approval remain separate.",
        preview.pack_id, preview.version, preview.publisher_id,
        preview.key_fingerprint, capabilities, contracts, preview.artifact_digest, update_review,
    );
    if let Some(section) = &preview.key_version_authorization {
        section.validate(preview, now_ms)?;
        message.push_str(&format!(
            "\n\nAdditive publisher key-version authorization\nSchema: {}\nPublisher: {}\nExact Pack: {} {}\nArtifact: {}\nSigned manifest: {}\nExisting key fingerprint: {}\nNew key fingerprint: {}\nNew key ID: {}\nPredecessor: {}\nCompatibility: {}\nPolicy generation: {}\nPolicy digest: {}\nPublisher keyset digest: {}\nHost owner: {}\nActive Profile revision: {}\nActive plan: {}\nActivation: {}\nSecurityEpoch: {}\nFence: {}\nIssued at (Unix ms): {}\nExpires at (Unix ms): {}\nOne-use challenge: {}\nAuthorization digest: {}\n\nThis adds permission for the new key only for the displayed exact Pack, version, artifact and signed manifest. Existing keys remain unchanged. This review expires within five minutes and can be used only once. It does not activate the Pack or grant capability approval.",
            section.schema, section.publisher_id, section.pack_id, section.version,
            section.artifact_digest, section.signed_manifest_digest,
            section.previous_key_fingerprint, section.new_key_fingerprint, section.new_key_id,
            section.predecessor_artifact_digest, section.compatibility_digest,
            section.policy_generation, section.policy_digest, section.publisher_keyset_digest,
            section.host_owner_id, section.profile_revision, section.plan_digest,
            section.activation_id, section.security_epoch, section.fencing_token,
            section.issued_at_ms, section.expires_at_ms, section.challenge,
            section.authorization_digest,
        ));
    }
    Ok(message)
}

/// Bind only a genuinely confirmed preview; the caller owns the Native choice.
pub(crate) fn commit_payload(
    preview: &NativePackOnboardingPreview,
    source_root: &Path,
    public_key_path: &Path,
    now_ms: u64,
) -> Result<Value, String> {
    let mut payload = json!({
        "source_root": source_root,
        "public_key_path": public_key_path,
        "preview_digest": preview.preview_digest,
    });
    if let Some(section) = &preview.key_version_authorization {
        // Recheck after the blocking choice; an expired review sends no commit.
        section.validate(preview, now_ms)?;
        payload["key_version_authorization_challenge"] = json!(section.challenge);
        payload["key_version_authorization_digest"] = json!(section.authorization_digest);
    }
    Ok(payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn digest(byte: char) -> String {
        format!("sha256:{}", byte.to_string().repeat(64))
    }

    fn preview_value(with_authorization: bool) -> Value {
        let mut value = json!({
            "pack_id": "rumi_provider_adapters_pack", "version": "1.0.1",
            "publisher_id": "publisher.example", "key_id": "a".repeat(32),
            "key_fingerprint": digest('a'), "requested_capabilities": [],
            "contract_versions": {}, "artifact_digest": digest('a'),
            "preview_digest": digest('c'), "predecessor_artifact_digest": digest('b'),
        });
        if with_authorization {
            value["key_version_authorization"] = json!({
                "schema": KEY_VERSION_SCHEMA, "publisher_id": value["publisher_id"],
                "pack_id": value["pack_id"], "version": value["version"],
                "artifact_digest": value["artifact_digest"],
                "signed_manifest_digest": digest('c'),
                "previous_key_fingerprint": digest('b'),
                "new_key_fingerprint": value["key_fingerprint"],
                "new_key_id": value["key_id"], "predecessor_artifact_digest": digest('b'),
                "compatibility_digest": digest('d'), "policy_digest": digest('e'),
                "publisher_keyset_digest": digest('f'), "host_owner_id": digest('1'),
                "profile_revision": digest('2'), "plan_digest": digest('3'),
                "activation_id": "activation:defaults-reviewed", "policy_generation": 0,
                "security_epoch": 3, "fencing_token": 5,
                "issued_at_ms": 1_000_000, "expires_at_ms": 1_300_000,
                "challenge": "one_use-Challenge_123456789", "authorization_digest": digest('0'),
            });
            resign(&mut value);
        }
        value
    }

    fn resign(value: &mut Value) {
        let section: KeyVersionAuthorization =
            serde_json::from_value(value["key_version_authorization"].clone()).unwrap();
        value["key_version_authorization"]["authorization_digest"] =
            json!(section.digest().unwrap());
    }

    fn parsed(value: Value) -> NativePackOnboardingPreview {
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn legacy_review_and_three_field_commit_remain_unchanged() {
        let preview = parsed(preview_value(false));
        let expected = format!(
            "Trust this separately selected publisher key for this signed Pack and add it?\n\nPack: rumi_provider_adapters_pack 1.0.1\nPublisher: publisher.example\nKey fingerprint: {}\nCapabilities: none\nContracts: none\nArtifact: {}\nRetained predecessor: {}\nThis adds a new revision. Existing Profiles keep their selected artifact until you review and activate an update.\n\n\nThis grants trust for this exact Pack identity and artifact. Installation and capability approval remain separate.",
            digest('a'), digest('a'), digest('b'),
        );
        assert_eq!(review_message(&preview, 1_000_001).unwrap(), expected);
        assert_eq!(
            commit_payload(
                &preview,
                Path::new("/pack"),
                Path::new("/key.pem"),
                1_000_001
            )
            .unwrap(),
            json!({
                "source_root": "/pack", "public_key_path": "/key.pem", "preview_digest": digest('c')
            })
        );
    }

    #[test]
    fn additive_review_displays_every_authorization_fact_and_exact_commit_binding() {
        let value = preview_value(true);
        // Independently computed with Python's sorted UTF-8 canonical JSON.
        assert_eq!(
            value["key_version_authorization"]["authorization_digest"],
            "sha256:9aca019743a0797ef5eedaed92b2a5dd423b00487180aa6110e499e929abdaf0"
        );
        let preview = parsed(value.clone());
        let message = review_message(&preview, 1_000_001).unwrap();
        for (key, fact) in value["key_version_authorization"].as_object().unwrap() {
            let text = fact
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| fact.to_string());
            assert!(message.contains(&text), "missing review fact {key}");
        }
        assert!(message.contains("Existing keys remain unchanged"));
        assert!(message.contains("five minutes and can be used only once"));
        let payload = commit_payload(
            &preview,
            Path::new("/pack"),
            Path::new("/key.pem"),
            1_000_001,
        )
        .unwrap();
        assert_eq!(payload.as_object().unwrap().len(), 5);
        assert_eq!(
            payload["key_version_authorization_challenge"],
            value["key_version_authorization"]["challenge"]
        );
        assert_eq!(
            payload["key_version_authorization_digest"],
            value["key_version_authorization"]["authorization_digest"]
        );
        assert!(payload.get("approved").is_none());
    }

    #[test]
    fn mismatched_outer_subjects_cannot_be_confirmed_or_committed() {
        for field in [
            "pack_id",
            "version",
            "publisher_id",
            "artifact_digest",
            "key_fingerprint",
            "key_id",
            "predecessor_artifact_digest",
        ] {
            let mut value = preview_value(true);
            value[field] = json!("different");
            let preview = parsed(value);
            assert!(review_message(&preview, 1_000_001).is_err(), "{field}");
            assert!(
                commit_payload(
                    &preview,
                    Path::new("/pack"),
                    Path::new("/key.pem"),
                    1_000_001
                )
                .is_err(),
                "{field}"
            );
        }
    }

    #[test]
    fn malformed_digest_key_challenge_scope_and_expiry_are_rejected() {
        for (field, replacement) in [
            ("schema", json!("unknown")),
            ("artifact_digest", json!("sha256:bad")),
            ("signed_manifest_digest", json!("sha256:bad")),
            ("previous_key_fingerprint", json!("sha256:bad")),
            ("new_key_fingerprint", json!("sha256:bad")),
            ("predecessor_artifact_digest", json!("sha256:bad")),
            ("compatibility_digest", json!("sha256:bad")),
            ("policy_digest", json!("sha256:bad")),
            ("publisher_keyset_digest", json!("sha256:bad")),
            ("host_owner_id", json!("sha256:bad")),
            ("profile_revision", json!("sha256:bad")),
            ("plan_digest", json!("sha256:bad")),
            ("new_key_id", json!("A".repeat(32))),
            ("activation_id", json!("activation:\nhidden")),
            ("challenge", json!("too-short")),
            ("challenge", json!("x".repeat(129))),
            ("challenge", json!("invalid/challenge-123456")),
            ("security_epoch", json!(0)),
            ("fencing_token", json!(0)),
            ("expires_at_ms", json!(999_999)),
            ("expires_at_ms", json!(1_300_001)),
        ] {
            let mut value = preview_value(true);
            value["key_version_authorization"][field] = replacement;
            resign(&mut value);
            assert!(
                review_message(&parsed(value), 1_000_001).is_err(),
                "{field}"
            );
        }
    }

    #[test]
    fn changed_authorization_digest_or_facts_fail_before_native_choice() {
        for field in ["authorization_digest", "policy_digest"] {
            let mut value = preview_value(true);
            value["key_version_authorization"][field] = json!(digest('9'));
            assert!(
                review_message(&parsed(value), 1_000_001).is_err(),
                "{field}"
            );
        }
    }

    #[test]
    fn missing_unknown_or_wrong_type_section_fields_never_fall_back_to_legacy() {
        let valid = preview_value(true);
        for field in valid["key_version_authorization"]
            .as_object()
            .unwrap()
            .keys()
        {
            let mut value = valid.clone();
            value["key_version_authorization"]
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(
                serde_json::from_value::<NativePackOnboardingPreview>(value).is_err(),
                "{field}"
            );
        }
        for (field, replacement) in [
            ("security_epoch", json!(true)),
            ("issued_at_ms", json!(-1)),
            ("policy_generation", json!("0")),
            ("unexpected", json!("extra")),
        ] {
            let mut value = valid.clone();
            value["key_version_authorization"][field] = replacement;
            assert!(
                serde_json::from_value::<NativePackOnboardingPreview>(value).is_err(),
                "{field}"
            );
        }
    }

    #[test]
    fn future_review_or_expiry_during_blocking_choice_sends_no_commit() {
        let preview = parsed(preview_value(true));
        assert!(review_message(&preview, 999_999).is_err());
        assert!(review_message(&preview, 1_000_001).is_ok());
        assert!(commit_payload(
            &preview,
            Path::new("/pack"),
            Path::new("/key.pem"),
            1_300_000
        )
        .is_err());
    }
}
