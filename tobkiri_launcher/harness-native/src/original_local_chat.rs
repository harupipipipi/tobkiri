//! Exact stored Provider selection for the original Broker's local JSON effect.

use anyhow::{Result as AnyResult, bail, ensure};
use std::{sync::Arc, time::Instant};
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CoreBuffer, ids::ProfileId,
};
use tobkiri_gate::CoreCancellation;
use tobkiri_pack_provider_registry_rust::resolve_legacy_local_chat_address;
pub use tobkiri_pack_provider_registry_rust::{
    LegacyLocalChatSelection, LegacyLocalChatSourceError,
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
    let address = {
        let _charge = DocumentCharge::reserve(budget)?;
        let document = root
            .read_legacy_provider_registry(request.profile, REGISTRY_DOCUMENT_LIMIT)
            .map_err(|_| OriginalLocalChatError::RegistryUnavailable)?
            .ok_or(OriginalLocalChatError::RegistryUnavailable)?;
        resolve_legacy_local_chat_address(&document, request.profile.as_str(), &request.provider)?
    };
    let body = CoreBuffer::copy_from(request.body, budget).map_err(|_| Transport::Capacity)?;
    Ok(post_brokered_legacy_local_chat(
        LegacyLocalChatHttpRequest {
            address,
            body,
            deadline: request.deadline,
            cancellation: request.cancellation,
        },
        budget,
        check,
    )
    .await?)
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
    budget: &Arc<dyn AllocationBudget>,
) -> AnyResult<(Option<CoreBuffer>, NativeReadOnlyPort<'a, T>)> {
    ensure!(source.len() <= LEGACY_LOCAL_CHAT_REQUEST_LIMIT);
    let input = BudgetedString::copy_from(source, port.allocation_owners().component(), 1)?;
    let (output, response_port) = port
        .invoke_with_document_limit_then(&[Val::String(input)])
        .await?;
    Ok((
        adapter_document(&output, LEGACY_LOCAL_CHAT_REQUEST_LIMIT, budget)?,
        response_port,
    ))
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
