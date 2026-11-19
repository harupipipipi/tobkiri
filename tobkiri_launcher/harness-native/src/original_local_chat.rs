//! Exact stored Provider selection for the original Broker's local JSON effect.

use anyhow::{Result as AnyResult, bail, ensure};
use std::{net::SocketAddr, sync::Arc, time::Instant};
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CoreBuffer, ids::ProfileId,
};
use tobkiri_gate::CoreCancellation;
pub use tobkiri_pack_provider_registry_rust::{
    LegacyLocalChatSelection, LegacyLocalChatSourceError,
};
use tobkiri_pack_provider_registry_rust::{
    resolve_legacy_local_chat_address, resolve_legacy_local_chat_policy,
};
use tobkiri_store::DataRoot;
pub use tobkiri_wasm::LegacyLocalChatHttpCheck as OriginalLocalChatCheck;
use tobkiri_wasm::{
    LEGACY_LOCAL_CHAT_REQUEST_LIMIT, LEGACY_LOCAL_CHAT_RESPONSE_LIMIT, LegacyLocalChatHttpError,
    LegacyLocalChatHttpRequest, NativeReadOnlyPort, post_brokered_legacy_local_chat,
};
use wasmtime::component::{BudgetedString, Val};

#[cfg(test)]
mod tests;

const REGISTRY_DOCUMENT_LIMIT: usize = 1024 * 1024;
// Preserve the original ordered JSON, whose parser bounds the document to
// 100,000 nodes. Reserve its input and temporary tree before the first read.
const REGISTRY_DOCUMENT_WORK: u64 = 26 * 1024 * 1024;

/// A finite local request from the original captured Provider invocation.
pub struct OriginalLocalChatRequest<'a> {
    /// The execution Profile retained by the original signed Broker.
    pub profile: &'a ProfileId,
    /// The selected connection as observed by the original adapter.
    pub provider: LegacyLocalChatSelection<'a>,
    /// The original encoded request JSON.
    pub body: &'a [u8],
    /// The selected record's policy used by the optional Wasm compiler.
    pub expected_policy: Option<&'a OriginalLocalChatPolicy>,
    /// The earliest deadline of the original lease and Provider request.
    pub deadline: Instant,
    /// The tracked native worker's inherited cancellation.
    pub cancellation: &'a CoreCancellation,
}

/// Closed errors from the original Profile observation and one local POST.
#[derive(Debug, thiserror::Error)]
pub enum OriginalLocalChatError {
    /// The original Profile's persisted Registry is missing or unavailable.
    #[error("original local provider Registry is unavailable")]
    RegistryUnavailable,
    /// The persisted keyless connection differs from the signed Host observation.
    #[error(transparent)]
    Source(#[from] LegacyLocalChatSourceError),
    /// The original finite transport rejected or aborted the exchange.
    #[error(transparent)]
    Transport(#[from] LegacyLocalChatHttpError),
}

/// The exact selected address and effective policy, retained under the Host budget.
pub struct OriginalLocalChatPolicy {
    address: SocketAddr,
    document: CoreBuffer,
}

impl OriginalLocalChatPolicy {
    /// The normalized policy, or an empty string when the record has no policy.
    pub fn document(&self) -> Result<&str, OriginalLocalChatError> {
        self.document
            .as_str()
            .map_err(|_| LegacyLocalChatSourceError::InvalidDocument.into())
    }
}

/// Read the policy from the same physical record that determines the loopback address.
pub fn observe_original_local_chat_policy(
    root: &DataRoot,
    profile: &ProfileId,
    provider: &LegacyLocalChatSelection<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalLocalChatPolicy, OriginalLocalChatError> {
    let _charge = DocumentCharge::reserve(budget)?;
    let document = read_registry(root, profile)?;
    let snapshot = resolve_legacy_local_chat_policy(&document, profile.as_str(), provider)?;
    Ok(OriginalLocalChatPolicy {
        address: snapshot.address,
        document: CoreBuffer::copy_from(snapshot.policy.as_bytes(), budget)
            .map_err(|_| LegacyLocalChatHttpError::Capacity)?,
    })
}

fn read_registry(root: &DataRoot, profile: &ProfileId) -> Result<Vec<u8>, OriginalLocalChatError> {
    root.read_legacy_provider_registry(profile, REGISTRY_DOCUMENT_LIMIT)
        .map_err(|_| OriginalLocalChatError::RegistryUnavailable)?
        .ok_or(OriginalLocalChatError::RegistryUnavailable)
}

/// Resolve the current physical Registry behind the original retained authority
/// and forward its exact local POST. Revalidate that authority at dispatch.
///
/// The supplied check owns the authorization and any remote publication
/// handoff. Failures carry no fallback after a dispatch permission is offered.
pub async fn post_original_local_chat(
    root: &DataRoot,
    request: OriginalLocalChatRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    mut check: impl FnMut(OriginalLocalChatCheck) -> anyhow::Result<()>,
) -> Result<CoreBuffer, OriginalLocalChatError> {
    use LegacyLocalChatHttpError as Transport;
    if request.body.is_empty() || request.body.len() > LEGACY_LOCAL_CHAT_REQUEST_LIMIT {
        return Err(Transport::InvalidRequest.into());
    }
    if request.cancellation.is_cancelled() {
        return Err(Transport::Revoked.into());
    }
    if request.deadline <= Instant::now() {
        return Err(Transport::Deadline.into());
    }
    check(OriginalLocalChatCheck::Live).map_err(|_| Transport::Revoked)?;
    let address = if let Some(policy) = request.expected_policy {
        policy.address
    } else {
        let _charge = DocumentCharge::reserve(budget)?;
        let document = read_registry(root, request.profile)?;
        resolve_legacy_local_chat_address(&document, request.profile.as_str(), &request.provider)?
    };
    let body = CoreBuffer::copy_from(request.body, budget).map_err(|_| Transport::Capacity)?;
    let mut registry_error = None;
    let response = post_brokered_legacy_local_chat(
        LegacyLocalChatHttpRequest {
            address,
            body,
            deadline: request.deadline,
            cancellation: request.cancellation,
        },
        budget,
        |phase| {
            if phase == OriginalLocalChatCheck::Dispatch
                && let Some(expected) = request.expected_policy
            {
                let result = observe_original_local_chat_policy(
                    root,
                    request.profile,
                    &request.provider,
                    budget,
                )
                .and_then(|current| {
                    if current.address != expected.address
                        || current.document.as_bytes() != expected.document.as_bytes()
                    {
                        Err(LegacyLocalChatSourceError::SelectionChanged.into())
                    } else {
                        Ok(())
                    }
                });
                if let Err(error) = result {
                    registry_error = Some(error);
                    bail!("local Provider policy observation changed at dispatch");
                }
            }
            check(phase)
        },
    )
    .await;
    if let Some(error) = registry_error {
        return Err(error);
    }
    Ok(response?)
}

/// Project a completed response through the verified effect-free Adapter port.
/// Unsupported response syntax or representation retains the original bytes.
pub async fn project_original_local_chat<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    raw: &str,
    stream: bool,
    budget: &Arc<dyn AllocationBudget>,
) -> AnyResult<Option<CoreBuffer>> {
    ensure!(raw.len() <= LEGACY_LOCAL_CHAT_RESPONSE_LIMIT);
    let input = BudgetedString::copy_from(raw, port.allocation_owners().component(), 1)?;
    let output = port
        .invoke_with_document_limit(&[Val::String(input), Val::Bool(stream)])
        .await?;
    adapter_document(&output, LEGACY_LOCAL_CHAT_RESPONSE_LIMIT, budget)
}

/// Compile the selected Provider inputs and retain the response port.
pub async fn compile_original_local_chat<'a, T: Send + 'static>(
    port: NativeReadOnlyPort<'a, T>,
    source: &str,
    policy: Option<&OriginalLocalChatPolicy>,
    budget: &Arc<dyn AllocationBudget>,
) -> AnyResult<(Option<CoreBuffer>, NativeReadOnlyPort<'a, T>)> {
    ensure!(source.len() <= LEGACY_LOCAL_CHAT_REQUEST_LIMIT);
    let input = BudgetedString::copy_from(source, port.allocation_owners().component(), 1)?;
    let (output, response_port) = if let Some(policy) = policy {
        let policy =
            BudgetedString::copy_from(policy.document()?, port.allocation_owners().component(), 1)?;
        port.invoke_with_document_limit_then(&[Val::String(input), Val::String(policy)])
            .await?
    } else {
        port.invoke_with_document_limit_then(&[Val::String(input)])
            .await?
    };
    if policy.is_some()
        && let [Val::Result(Err(Some(value)))] = output.as_slice()
    {
        return Err(read_policy_error(value)?.into());
    }
    Ok((
        adapter_document(&output, LEGACY_LOCAL_CHAT_REQUEST_LIMIT, budget)?,
        response_port,
    ))
}

/// The closed denials that preserve the original local Adapter error messages.
#[derive(Clone, Copy, Debug, thiserror::Error)]
pub enum OriginalLocalChatPolicyError {
    /// The selected connection excludes the requested model.
    #[error("model is not permitted by this connection")]
    Denied,
    /// The normalized model ID is invalid.
    #[error("provider model ID is invalid")]
    Model,
    /// Parameters are not an object or override the selected route.
    #[error("request parameters cannot override the saved model route")]
    Parameters,
}

fn read_policy_error(value: &Val) -> AnyResult<OriginalLocalChatPolicyError> {
    let Val::Record(fields) = value else {
        bail!("local Provider Adapter returned a different policy error type");
    };
    let [(code, Val::Enum(kind)), (message, Val::String(text))] = fields.as_slice() else {
        bail!("local Provider Adapter returned a malformed policy error");
    };
    ensure!(code.as_str() == "code" && message.as_str() == "message");
    match (kind.as_str(), text.as_str()) {
        ("denied", "model is not permitted by this connection") => {
            Ok(OriginalLocalChatPolicyError::Denied)
        }
        ("invalid", "provider model ID is invalid") => Ok(OriginalLocalChatPolicyError::Model),
        ("invalid", "request parameters cannot override the saved model route") => {
            Ok(OriginalLocalChatPolicyError::Parameters)
        }
        _ => bail!("local Provider Adapter returned an unsupported policy error"),
    }
}

fn adapter_document(
    output: &[Val],
    limit: usize,
    budget: &Arc<dyn AllocationBudget>,
) -> AnyResult<Option<CoreBuffer>> {
    match output {
        [Val::Result(Ok(Some(value)))] => match &**value {
            Val::String(text) if text.as_str().is_empty() => Ok(None),
            Val::String(text) if text.as_str().len() <= limit && text.as_str().starts_with('{') => {
                Ok(Some(CoreBuffer::copy_from(
                    text.as_str().as_bytes(),
                    budget,
                )?))
            }
            _ => bail!("local Provider Adapter returned a different result type"),
        },
        _ => bail!("local Provider Adapter omitted its result"),
    }
}

struct DocumentCharge<'a>(&'a Arc<dyn AllocationBudget>);
impl<'a> DocumentCharge<'a> {
    fn reserve(budget: &'a Arc<dyn AllocationBudget>) -> Result<Self, LegacyLocalChatHttpError> {
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: REGISTRY_DOCUMENT_WORK,
                elements: 0,
                depth: 0,
                work: 0,
            })
            .map_err(|_| LegacyLocalChatHttpError::Capacity)?;
        Ok(Self(budget))
    }
}
impl Drop for DocumentCharge<'_> {
    fn drop(&mut self) {
        self.0.release(REGISTRY_DOCUMENT_WORK);
    }
}
