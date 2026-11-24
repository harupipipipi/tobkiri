// Generated verbatim source excerpts; provenance is vectors/seals/extra-provenance.json.
struct ParsedRequest {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}
pub(crate) struct BrokerAttestationIdentity {
    instance_nonce: String,
    signing_key: Arc<SigningKey>,
}
fn attest_response(
    request: &ParsedRequest,
    status_code: u16,
    body: &mut Value,
    identity: &BrokerAttestationIdentity,
) -> Result<()> {
    let Some(request_nonce) = request
        .headers
        .get(RESPONSE_NONCE_HEADER)
        .map(|value| value.trim())
        .filter(|value| (32..=256).contains(&value.len()))
    else {
        return Ok(());
    };
    let payload =
        serde_json::to_vec(body).context("failed to encode broker attestation payload")?;
    let payload_hash = hex::encode(Sha256::digest(&payload));
    let signed = format!(
        "tobkiri-launcher-response-v1\n{}\n{}\n{}\n{}\n{}\n{}",
        identity.instance_nonce,
        request_nonce,
        request.method,
        request.path,
        status_code,
        payload_hash,
    );
    let signature = identity.signing_key.sign(signed.as_bytes());
    let Some(object) = body.as_object_mut() else {
        bail!("broker response body must be a JSON object");
    };
    object.insert(
        "_launcher_attestation".to_string(),
        json!({
            "version": 1,
            "algorithm": "Ed25519",
            "instance_nonce": identity.instance_nonce,
            "request_nonce": request_nonce,
            "method": request.method,
            "path": request.path,
            "status": status_code,
            "payload_sha256": payload_hash,
            "payload": URL_SAFE_NO_PAD.encode(payload),
            "signature": URL_SAFE_NO_PAD.encode(signature.to_bytes()),
        }),
    );
    Ok(())
}
#[derive(serde::Deserialize)]
pub struct DebugCliOperator {
    pub kind: String,
    pub version: u8,
    pub origin: String,
    pub scope: String,
    pub session_id: String,
    pub run_id: String,
    pub workspace_digest: String,
    pub pack_id: String,
    pub profile_id: String,
    pub profile_revision: String,
    pub activation_id: String,
    pub plan_digest: String,
    pub lease_epoch: u64,
    pub request_id: String,
    pub permission_id: String,
    pub tool: String,
    pub action: String,
    pub operation: String,
    pub decision: String,
    pub canonical_arguments_digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_digest: Option<String>,
    pub conversation_id: String,
    pub operation_owner: String,
    pub issued_at: u64,
    pub expires_at: u64,
    pub nonce: String,
    pub signature: String,
}
fn canonical_operator_payload(operator: &DebugCliOperator) -> Result<String, String> {
    serde_json::to_string(&json!({
        "kind": operator.kind,
        "version": operator.version,
        "origin": operator.origin,
        "scope": operator.scope,
        "session_id": operator.session_id,
        "run_id": operator.run_id,
        "workspace_digest": operator.workspace_digest,
        "pack_id": operator.pack_id,
        "profile_id": operator.profile_id,
        "profile_revision": operator.profile_revision,
        "activation_id": operator.activation_id,
        "plan_digest": operator.plan_digest,
        "lease_epoch": operator.lease_epoch,
        "request_id": operator.request_id,
        "permission_id": operator.permission_id,
        "tool": operator.tool,
        "action": operator.action,
        "operation": operator.operation,
        "decision": operator.decision,
        "canonical_arguments_digest": operator.canonical_arguments_digest,
        "target_digest": operator.target_digest,
        "conversation_id": operator.conversation_id,
        "operation_owner": operator.operation_owner,
        "issued_at": operator.issued_at,
        "expires_at": operator.expires_at,
        "nonce": operator.nonce,
    }))
    .map_err(|error| format!("failed to encode debug operator: {error}"))
}
impl DebugApprovalManager {
fn sign(&self, operator: &DebugCliOperator) -> Result<String, String> {
        let unsigned = canonical_operator_payload(operator)?;
        let mut mac = HmacSha256::new_from_slice(&self.signing_key)
            .map_err(|_| "debug signing key unavailable")?;
        mac.update(unsigned.as_bytes());
        Ok(hex::encode(mac.finalize().into_bytes()))
    }
}
#[derive(serde::Deserialize)]
struct PresentationReleaseManifest {
    schema: String,
    catalog_path: String,
    catalog_sha256: String,
    artifact_index_path: String,
    artifact_index_sha256: String,
    profile_lock_path: String,
    profile_lock_sha256: String,
    default_profile_path: String,
    default_profile_sha256: String,
    defaultspack_lock_path: String,
    defaultspack_lock_sha256: String,
    artifact_id: String,
    platform: String,
    architecture: String,
    source_identity: String,
    source_revision: String,
    key_id: String,
    public_key: String,
    signature: String,
}
fn release_signature_message(manifest: &PresentationReleaseManifest) -> Vec<u8> {
    [
        RELEASE_SCHEMA,
        &manifest.catalog_sha256,
        &manifest.artifact_index_sha256,
        &manifest.profile_lock_sha256,
        &manifest.default_profile_sha256,
        &manifest.defaultspack_lock_sha256,
        &manifest.source_identity,
        &manifest.source_revision,
        &manifest.platform,
        &manifest.architecture,
        &manifest.artifact_id,
        &manifest.key_id,
    ]
    .join("\0")
    .into_bytes()
}
