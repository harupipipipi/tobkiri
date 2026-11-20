//! Funded calls into the stateless Saved Turn input projection port.

use super::{
    AllocationBudget, Arc, CoreBuffer, Digest, Error, NativeReadOnlyPort,
    OriginalSavedInputIdentity, OriginalSavedInputProjection, OriginalSavedInputRow, RECORD_LIMIT,
    Result, Val, decode_guest_error,
};
use wasmtime::component::{BudgetedString, HostAllocationOwner};

/// Validate the original input and identify its existing index key in Wasm.
pub async fn invoke_original_saved_input_identify_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    source: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedInputIdentity> {
    let argument = text(source, port.allocation_owners().component())?;
    let output = port.invoke(&[argument]).await.map_err(|_| Error::Storage)?;
    decode_identity(input_reply(output.as_slice())?, budget)
}

/// Validate one immutable projected input and produce its compact SQLite row.
pub async fn invoke_original_saved_input_prepare_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    source: &str,
    accepted: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedInputProjection> {
    let owner = port.allocation_owners().component();
    let parameters = [text(source, owner)?, text(accepted, owner)?];
    let output = port.invoke(&parameters).await.map_err(|_| Error::Storage)?;
    let Val::Record(fields) = input_reply(output.as_slice())? else {
        return Err(Error::Storage);
    };
    let [
        (identity_key, identity),
        (digest_key, Val::String(digest)),
        (context_key, Val::String(context)),
    ] = fields.as_slice()
    else {
        return Err(Error::Storage);
    };
    if identity_key.as_str() != "identity"
        || digest_key.as_str() != "accepted-digest"
        || context_key.as_str() != "context"
    {
        return Err(Error::Storage);
    }
    if context.as_str().len() > RECORD_LIMIT {
        return Err(Error::Capacity);
    }
    Ok(OriginalSavedInputProjection {
        identity: decode_identity(identity, budget)?,
        accepted_digest: Digest::parse(digest.as_str()).map_err(|_| Error::InvalidRecord)?,
        context: copy_text(context.as_str(), budget)?,
    })
}

/// Restore the validated input represented by one committed or proposed row.
pub async fn invoke_original_saved_input_recover_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    source: &str,
    row: OriginalSavedInputRow<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    if row.context.len() > RECORD_LIMIT {
        return Err(Error::Capacity);
    }
    let owner = port.allocation_owners().component();
    let parameters = [
        text(source, owner)?,
        text(row.source_digest, owner)?,
        text(row.accepted_digest, owner)?,
        text(row.context, owner)?,
    ];
    let output = port.invoke(&parameters).await.map_err(|_| Error::Storage)?;
    let Val::String(body) = input_reply(output.as_slice())? else {
        return Err(Error::Storage);
    };
    copy_text(body.as_str(), budget)
}

fn input_reply(output: &[Val]) -> Result<&Val> {
    let [Val::Result(Ok(Some(outcome)))] = output else {
        return Err(Error::Storage);
    };
    match &**outcome {
        Val::Result(Ok(Some(value))) => Ok(value),
        Val::Result(Err(Some(code))) => match &**code {
            Val::Enum(code) => Err(decode_guest_error(code.as_str())),
            _ => Err(Error::Storage),
        },
        _ => Err(Error::Storage),
    }
}

fn decode_identity(
    value: &Val,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedInputIdentity> {
    let Val::Record(fields) = value else {
        return Err(Error::Storage);
    };
    let [(id_key, Val::String(id)), (digest_key, Val::String(digest))] = fields.as_slice() else {
        return Err(Error::Storage);
    };
    if id_key.as_str() != "turn-id" || digest_key.as_str() != "source-digest" {
        return Err(Error::Storage);
    }
    let id = id.as_str();
    if id.is_empty() || id.len() > 256 {
        return Err(Error::InvalidRecord);
    }
    Ok(OriginalSavedInputIdentity {
        turn_id: copy_text(id, budget)?,
        source_digest: Digest::parse(digest.as_str()).map_err(|_| Error::InvalidRecord)?,
    })
}

fn text(value: &str, owner: &HostAllocationOwner) -> Result<Val> {
    BudgetedString::copy_from(value, owner, 1)
        .map(Val::String)
        .map_err(|_| Error::Capacity)
}

fn copy_text(value: &str, budget: &Arc<dyn AllocationBudget>) -> Result<CoreBuffer> {
    CoreBuffer::copy_from(value.as_bytes(), budget).map_err(|_| Error::Capacity)
}
