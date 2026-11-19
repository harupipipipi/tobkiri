//! The saved-orchestration guest's single, pure continuation step.

use anyhow::{Result, bail};
use tobkiri_gate::NativeSetupReservation;
use tobkiri_wasm::NativeReadOnlyPort;
use wasmtime::component::{BudgetedString, Val};

/// A typed failure at the saved-orchestration DATA boundary.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SavedOrchestrationStepError {
    /// The step rejected its input.
    Invalid,
    /// The request or result exceeded the available capacity.
    Capacity,
}

impl std::fmt::Display for SavedOrchestrationStepError {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str(match self {
            Self::Invalid => "saved orchestration input is invalid",
            Self::Capacity => "saved orchestration step exceeded capacity",
        })
    }
}

impl std::error::Error for SavedOrchestrationStepError {}

/// Move the transport request into funded Wasm arguments and evaluate once.
///
/// The returned string retains its original component allocation and carries
/// only the guest's DATA for the original owner to examine.
pub async fn invoke_original_saved_orchestration_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    input: Vec<u8>,
    input_funding: NativeSetupReservation,
) -> Result<BudgetedString> {
    let text = std::str::from_utf8(&input).map_err(|_| SavedOrchestrationStepError::Invalid)?;
    let text = BudgetedString::copy_from(text, port.allocation_owners().component(), 1)
        .map_err(|_| SavedOrchestrationStepError::Capacity)?;
    drop(input);
    drop(input_funding);

    let parameters = [Val::String(text)];
    let output = port.invoke_with_document_limit(&parameters).await?;
    match output.as_slice() {
        [Val::Result(Ok(Some(result)))] => match &**result {
            Val::String(text) => Ok(text.clone()),
            _ => bail!("saved orchestration port returned a different result type"),
        },
        [Val::Result(Err(Some(result)))] => {
            let Val::Record(fields) = &**result else {
                bail!("saved orchestration port returned a different error type");
            };
            let [(code, Val::Enum(tag)), (message, Val::String(_))] = fields.as_slice() else {
                bail!("saved orchestration port returned an invalid error record");
            };
            let error = match (code.as_str(), tag.as_str(), message.as_str()) {
                ("code", "invalid", "message") => SavedOrchestrationStepError::Invalid,
                ("code", "limit-exceeded", "message") => SavedOrchestrationStepError::Capacity,
                _ => bail!("saved orchestration port returned an unsupported error code"),
            };
            Err(error.into())
        }
        _ => bail!("saved orchestration port omitted its result"),
    }
}
