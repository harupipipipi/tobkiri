//! Bounded calls into the admitted saved-turn start component.

use super::{
    Error, LegacySavedTurnStartRequest, OriginalSavedTurnDecision, OriginalSavedTurnRow, Result,
    decode_port_decision,
};
use std::sync::Arc;
use tobkiri_canon::AllocationBudget;
use tobkiri_wasm::NativeReadOnlyPort;
use wasmtime::component::{BudgetedString, HostAllocationOwner, Val};

/// Prepare the queued row in Wasm for the already admitted Profile.
pub async fn invoke_original_saved_turn_begin_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    request: &LegacySavedTurnStartRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedTurnDecision> {
    let owner = port.allocation_owners().component();
    let parameters = [
        text(request.turn_id, owner)?,
        text(request.conversation_id, owner)?,
        Val::U64(request.conversation_revision),
        text(request.input_digest, owner)?,
    ];
    let output = port.invoke(&parameters).await.map_err(|_| Error::Storage)?;
    decode_port_decision(output.as_slice(), budget)
}

/// Prepare the running row in Wasm for an observation held under the Turn lock.
pub async fn invoke_original_saved_turn_claim_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    row: OriginalSavedTurnRow<'_>,
    request: &LegacySavedTurnStartRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedTurnDecision> {
    if row.turn_id != request.turn_id {
        return Err(Error::InvalidRecord);
    }
    let owner = port.allocation_owners().component();
    let parameters = [
        text(row.turn_id, owner)?,
        text(row.request_id, owner)?,
        text(row.body, owner)?,
        text(request.conversation_id, owner)?,
        Val::U64(request.conversation_revision),
        text(request.input_digest, owner)?,
    ];
    let output = port.invoke(&parameters).await.map_err(|_| Error::Storage)?;
    decode_port_decision(output.as_slice(), budget)
}

fn text(value: &str, owner: &HostAllocationOwner) -> Result<Val> {
    BudgetedString::copy_from(value, owner, 1)
        .map(Val::String)
        .map_err(|_| Error::Capacity)
}
