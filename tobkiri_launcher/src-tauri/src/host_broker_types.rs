use serde::{Deserialize, Serialize};
use serde_json::Value;

// The broker may receive a result from an older helper during a rolling
// update. Normalize only this legacy input spelling before returning a result
// to the current defaults profile; all current helpers emit the narrow branch
// code below.
pub const TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE: &str =
    "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE";
pub const LEGACY_TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE: &str =
    "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE";
pub const TYPE_ACCESSIBILITY_API_UNAVAILABLE: &str = "TYPE_ACCESSIBILITY_API_UNAVAILABLE";
pub const TYPE_SEMANTIC_PROTOCOL_INVALID: &str = "TYPE_SEMANTIC_PROTOCOL_INVALID";

pub fn canonical_type_semantic_error_code(code: &str) -> Option<&'static str> {
    match code {
        TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE
        | LEGACY_TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE => {
            Some(TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE)
        }
        TYPE_ACCESSIBILITY_API_UNAVAILABLE => Some(TYPE_ACCESSIBILITY_API_UNAVAILABLE),
        TYPE_SEMANTIC_PROTOCOL_INVALID => Some(TYPE_SEMANTIC_PROTOCOL_INVALID),
        _ => None,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HostBrokerStatus {
    pub enabled: bool,
    pub available: bool,
    pub status: String,
    pub url: Option<String>,
    pub connection_path: Option<String>,
    pub recovery: Option<String>,
}

impl HostBrokerStatus {
    pub fn disabled(reason: &str) -> Self {
        Self {
            enabled: false,
            available: false,
            status: "disabled".to_string(),
            url: None,
            connection_path: None,
            recovery: Some(reason.to_string()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HostBrokerConnectionInfo {
    pub version: u32,
    pub host: String,
    pub port: u16,
    pub url: String,
    pub token: String,
    pub permission_subject: String,
    pub pid: u32,
    pub created_at: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub instance_nonce: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attestation_public_key: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attestation_instance_nonce: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HostBrokerComputerRunRequest {
    pub function_id: String,
    #[serde(default)]
    pub profile_id: Option<String>,
    #[serde(default)]
    pub pack_id: Option<String>,
    #[serde(default)]
    pub conversation_id: Option<String>,
    #[serde(default)]
    pub approval_token: Option<String>,
    #[serde(default)]
    pub artifact_root: Option<String>,
    #[serde(default)]
    pub args: Value,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HostBrokerIntentCaller {
    #[serde(default)]
    pub pack_id: Option<String>,
    #[serde(default)]
    pub function_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HostBrokerIntentRequest {
    #[serde(rename = "type", default)]
    pub intent_type: String,
    #[serde(default)]
    pub operation: String,
    #[serde(default)]
    pub args: Value,
    #[serde(default)]
    pub stream: Value,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub caller: Option<HostBrokerIntentCaller>,
    #[serde(default)]
    pub conversation_id: Option<String>,
    #[serde(default)]
    pub host_function_id: Option<String>,
    #[serde(default)]
    pub approval_token: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HostBrokerStreamStopRequest {
    pub stream_id: String,
    #[serde(default)]
    pub operation: Option<String>,
    #[serde(default)]
    pub caller: Option<HostBrokerIntentCaller>,
    #[serde(default)]
    pub conversation_id: Option<String>,
    #[serde(default)]
    pub stop_token: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct HostBrokerError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct HostBrokerComputerRunResponse {
    pub ok: bool,
    pub function_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostics: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<HostBrokerError>,
    pub audit_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct HostBrokerIntentResponse {
    pub ok: bool,
    pub operation: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<HostBrokerError>,
    pub audit_id: String,
}
