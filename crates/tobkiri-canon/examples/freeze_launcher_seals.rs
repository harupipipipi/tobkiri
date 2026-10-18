//! IO-free oracle for verbatim legacy source excerpts. This is fixture tooling.
#![allow(dead_code)]
use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use ed25519_dalek::{Signature, Signer};
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};
type Result<T, E = String> = std::result::Result<T, E>;
type HmacSha256 = Hmac<Sha256>;
const RESPONSE_NONCE_HEADER: &str = "x-rumi-launcher-response-nonce";
const RELEASE_SCHEMA: &str = "io.tobkiri.shell.release.v4";
macro_rules! bail {
    ($message:expr) => {
        return Err($message.into())
    };
}
trait Context<T> {
    fn context(self, message: &str) -> Result<T>;
}
impl<T, E: std::fmt::Display> Context<T> for std::result::Result<T, E> {
    fn context(self, message: &str) -> Result<T> {
        self.map_err(|e| format!("{message}: {e}"))
    }
}
mod hex {
    pub fn encode(bytes: impl AsRef<[u8]>) -> String {
        bytes.as_ref().iter().map(|b| format!("{b:02x}")).collect()
    }
}
// Record exactly the bytes passed by the original function to its signer,
// then delegate cryptography to the same Ed25519 primitive with a fixed seed.
struct SigningKey {
    inner: ed25519_dalek::SigningKey,
    message: Mutex<Vec<u8>>,
}
impl SigningKey {
    fn fixture() -> Self {
        Self {
            inner: ed25519_dalek::SigningKey::from_bytes(&std::array::from_fn(|i| i as u8)),
            message: Mutex::new(vec![]),
        }
    }
    fn sign(&self, message: &[u8]) -> Signature {
        *self.message.lock().unwrap() = message.to_vec();
        self.inner.sign(message)
    }
}
struct DebugApprovalManager {
    signing_key: [u8; 32],
}
include!("../../../tools/legacy-vectors/launcher_source.rs");

fn main() {
    let signer = Arc::new(SigningKey::fixture());
    let manager = DebugApprovalManager {
        signing_key: std::array::from_fn(|i| i as u8),
    };
    let mut launcher = vec![];
    let mut debug = vec![];
    let mut release = vec![];
    for index in 0..300u64 {
        let identity = BrokerAttestationIdentity {
            instance_nonce: format!("fixture-instance-{index}"),
            signing_key: signer.clone(),
        };
        let request = ParsedRequest {
            method: "POST".into(),
            path: format!("/fixture/あ🚀/{index}"),
            headers: HashMap::from([(RESPONSE_NONCE_HEADER.into(), format!("{index:048x}"))]),
            body: vec![],
        };
        let mut body = json!({"fixture":index,"unicode":"あ🚀","ok":true});
        attest_response(&request, 200, &mut body, &identity).unwrap();
        launcher.push(json!({"input":body,"message_b64":STANDARD.encode(&*signer.message.lock().unwrap()),"public_key_b64url":URL_SAFE_NO_PAD.encode(signer.inner.verifying_key().to_bytes()),"test_only_seed_hex":hex::encode(std::array::from_fn::<_,32,_>(|i|i as u8)),"source_call":"attest_response"}));
        let mut input = serde_json::Map::new();
        for name in [
            "kind",
            "origin",
            "scope",
            "session_id",
            "run_id",
            "workspace_digest",
            "pack_id",
            "profile_id",
            "profile_revision",
            "activation_id",
            "plan_digest",
            "request_id",
            "permission_id",
            "tool",
            "action",
            "operation",
            "decision",
            "canonical_arguments_digest",
            "conversation_id",
            "operation_owner",
            "nonce",
            "signature",
        ] {
            input.insert(name.into(), json!(format!("fixture-{index}-{name}-あ🚀")));
        }
        input.insert("version".into(), json!(1));
        input.insert("lease_epoch".into(), json!(index));
        input.insert("issued_at".into(), json!(1750000000 + index));
        input.insert("expires_at".into(), json!(1750000900 + index));
        input.insert(
            "target_digest".into(),
            if index % 2 == 0 {
                Value::Null
            } else {
                json!("a".repeat(64))
            },
        );
        let operator: DebugCliOperator =
            serde_json::from_value(Value::Object(input.clone())).unwrap();
        let payload = canonical_operator_payload(&operator).unwrap();
        debug.push(json!({"input":input,"message_b64":STANDARD.encode(payload.as_bytes()),"hmac_hex":manager.sign(&operator).unwrap(),"test_only_key_hex":hex::encode(manager.signing_key),"source_call":"DebugApprovalManager::sign"}));
        let mut input = serde_json::Map::new();
        for name in [
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
        ] {
            input.insert(name.into(), json!(format!("fixture-{index}-{name}")));
        }
        input["source_identity"] = json!(format!("fixture-{index}-source_identity-あ🚀"));
        let manifest: PresentationReleaseManifest =
            serde_json::from_value(Value::Object(input.clone())).unwrap();
        let message = release_signature_message(&manifest);
        release.push(json!({"input":input,"message_b64":STANDARD.encode(&message),"source_call":"release_signature_message"}));
    }
    println!(
        "{}",
        json!({"launcher-response":launcher,"debug-cli-operator":debug,"release-rust":release})
    );
}
