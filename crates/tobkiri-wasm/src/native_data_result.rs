//! Typed nativeKit result DATA. This is not a Core/Gate error or authority.
use super::{NativeDataEntry, NativeDataObservation, NativeDataReply};
use anyhow::{Context, Result, bail};
use std::time::Instant;
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreBuffer, MAX_BYTES};
use wasmtime::component::Val;
use wasmtime::component::types::Type;

/// Exact successful-result contract supplied by trusted nativeKit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeDataSuccessType {
    /// A UTF-8 DATA envelope with the original string boundary.
    String,
    /// No success payload, as required by explicit initialization operations.
    Unit,
}

/// Borrowed nativeKit error schema. Names are compared exactly, never inferred
/// from a Core/Pack identity, and are not retained or copied by the runner.
#[derive(Clone, Copy)]
pub struct NativeDataErrorContract<'a> {
    /// First record field: the enum-valued error code.
    pub code_field: &'a str,
    /// Second record field: the UTF-8 error message.
    pub message_field: &'a str,
    /// Optional third UTF-8 field, selected before binding. The nativeKit owns
    /// its closed DATA decoder; the runtime never interprets or grants it.
    /// Some requires exactly this field, without falling back to the old ABI.
    pub details_field: Option<&'a str>,
    /// Exact enum cases, including their original order.
    pub cases: &'a [&'a str],
}

/// Complete native result contract, independent of ordinary Pack nominal types.
#[derive(Clone, Copy)]
pub struct NativeDataResultContract<'a> {
    /// Required success payload, selected before instantiation.
    pub success: NativeDataSuccessType,
    /// Exact nativeKit domain-error contract.
    pub error: NativeDataErrorContract<'a>,
}

impl NativeDataResultContract<'_> {
    pub(super) fn charge(&self, owners: &crate::AllocationOwners) -> Result<()> {
        let charge = |bytes: usize| -> Result<()> {
            let work = u64::try_from(bytes)?
                .checked_mul(4)
                .and_then(|n| n.checked_add(1))
                .context("native result schema work overflow")?;
            owners.graph().reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: 0,
                elements: 0,
                depth: 0,
                work,
            })?;
            Ok(())
        };
        charge(self.error.code_field.len())?;
        charge(self.error.message_field.len())?;
        if let Some(name) = self.error.details_field {
            charge(name.len())?;
        }
        for name in self.error.cases {
            charge(name.len())?;
        }
        Ok(())
    }
}

/// Successful native DATA, with no grant or activation meaning.
#[derive(Debug)]
pub enum NativeDataValue {
    /// An immutable string funded by the original invocation.
    String(CoreBuffer),
    /// A correctly typed empty success result.
    Unit,
}

/// Untrusted domain-error DATA. It deliberately does not implement Error or
/// convert to a Gate error: even an `approval-required` code grants nothing.
pub struct NativeDataError {
    code: CoreBuffer,
    message: CoreBuffer,
    details: Option<CoreBuffer>,
}
impl NativeDataError {
    /// Borrow the still-funded UTF-8 enum spelling.
    pub fn code(&self) -> &CoreBuffer {
        &self.code
    }
    /// Borrow the still-funded UTF-8 message without formatting or copying it.
    pub fn message(&self) -> &CoreBuffer {
        &self.message
    }
    /// Borrow the still-funded opaque details, if the selected ABI requires
    /// them. Their original nativeKit schema must be validated before use.
    pub fn details(&self) -> Option<&CoreBuffer> {
        self.details.as_ref()
    }
    /// Transfer the original immutable buffers, retaining their original owner.
    pub fn into_parts(self) -> (CoreBuffer, CoreBuffer, Option<CoreBuffer>) {
        (self.code, self.message, self.details)
    }
}
impl std::fmt::Debug for NativeDataError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NativeDataError")
            .field("code_bytes", &self.code.len())
            .field("message_bytes", &self.message.len())
            .field("details_bytes", &self.details.as_ref().map(CoreBuffer::len))
            .finish()
    }
}

/// Result-shaped native entry. Ordinary domain errors complete post-return and
/// keep the original instance; traps/ABI/post-return failures retire it.
pub struct NativeDataResultEntry<T: 'static> {
    entry: NativeDataEntry<T>,
}
impl<T: 'static> NativeDataResultEntry<T> {
    pub(super) fn new(entry: NativeDataEntry<T>) -> Self {
        Self { entry }
    }
    /// Retire and actually destroy the retained Store.
    pub fn retire(&mut self) {
        self.entry.retire();
    }
}
impl<T: Send + 'static> NativeDataResultEntry<T> {
    /// Call the exact bound function. The outer error means an execution/ABI
    /// failure; the inner error is only the nativeKit's original domain DATA.
    pub async fn invoke(
        &mut self,
        input: Option<&CoreBuffer>,
    ) -> Result<std::result::Result<NativeDataValue, NativeDataError>> {
        match self.entry.call(input).await? {
            NativeDataReply::Value(value) => Ok(Ok(value)),
            NativeDataReply::DomainError(error) => Ok(Err(error)),
        }
    }
    /// Inspect the original absolute deadline without renewing it.
    pub fn original_deadline(&self) -> Instant {
        self.entry.original_deadline()
    }
    /// Inspect remaining original fuel without extracting the Store.
    pub fn fuel_remaining(&self) -> Result<u64> {
        self.entry.fuel_remaining()
    }
    /// Borrow bounded observation DATA, including after actual retirement.
    pub fn observation(&self) -> NativeDataObservation {
        self.entry.observation()
    }
}

pub(super) fn validate(ty: &Type, contract: NativeDataResultContract<'_>) -> Result<()> {
    let Type::Result(result) = ty else {
        bail!("native DATA export must return the selected result type");
    };
    let expected = match contract.success {
        NativeDataSuccessType::String => Some(Type::String),
        NativeDataSuccessType::Unit => None,
    };
    if result.ok() != expected {
        bail!("native DATA result success payload differs");
    }
    let Some(Type::Record(record)) = result.err() else {
        bail!("native DATA result error must be a record");
    };
    let mut fields = record.fields();
    let (Some(code), Some(message)) = (fields.next(), fields.next()) else {
        bail!("native DATA error fields differ");
    };
    if code.name != contract.error.code_field
        || message.name != contract.error.message_field
        || message.ty != Type::String
        || contract.error.cases.is_empty()
    {
        bail!("native DATA error fields differ");
    }
    match (contract.error.details_field, fields.next()) {
        (None, None) => {}
        (Some(name), Some(field)) if field.name == name && field.ty == Type::String => {}
        _ => bail!("native DATA error details field differs"),
    }
    if fields.next().is_some() {
        bail!("native DATA error has extra fields");
    }
    let Type::Enum(cases) = code.ty else {
        bail!("native DATA error code must be an enum");
    };
    if !cases.names().eq(contract.error.cases.iter().copied()) {
        bail!("native DATA error enum values or order differ");
    }
    Ok(())
}

pub(super) fn string(text: &str, owners: &crate::AllocationOwners) -> Result<CoreBuffer> {
    if text.len() > MAX_BYTES {
        bail!("native DATA output exceeds the 4 MiB string envelope");
    }
    Ok(CoreBuffer::copy_from(text.as_bytes(), owners.graph())?)
}

pub(super) fn decode(
    values: &[Val],
    success: NativeDataSuccessType,
    details: bool,
    owners: &crate::AllocationOwners,
) -> Result<NativeDataReply> {
    let [Val::Result(result)] = values else {
        bail!("native DATA export returned an unexpected result value");
    };
    match (result, success) {
        (Ok(None), NativeDataSuccessType::Unit) => {
            Ok(NativeDataReply::Value(NativeDataValue::Unit))
        }
        (Ok(Some(value)), NativeDataSuccessType::String) => {
            let Val::String(text) = value.as_ref() else {
                bail!("native DATA result success value differs");
            };
            Ok(NativeDataReply::Value(NativeDataValue::String(string(
                text.as_str(),
                owners,
            )?)))
        }
        (Err(Some(value)), _) => {
            let Val::Record(fields) = value.as_ref() else {
                bail!("native DATA result error value differs");
            };
            // Field identities/types/cases were verified against the immutable
            // component signature. Owned canonical lifting preserves that order
            // and rejects unknown discriminants before reaching this decoder.
            let (code, message, details) = match (details, fields.as_slice()) {
                (false, [(_, Val::Enum(code)), (_, Val::String(message))]) => (code, message, None),
                (
                    true,
                    [
                        (_, Val::Enum(code)),
                        (_, Val::String(message)),
                        (_, Val::String(details)),
                    ],
                ) => (code, message, Some(details)),
                _ => bail!("native DATA result error value differs"),
            };
            let bytes = code
                .as_str()
                .len()
                .checked_add(message.as_str().len())
                .and_then(|n| n.checked_add(details.map_or(0, |value| value.as_str().len())))
                .context("native DATA error size overflow")?;
            if bytes > MAX_BYTES {
                bail!("native DATA error exceeds the original 4 MiB envelope");
            }
            Ok(NativeDataReply::DomainError(NativeDataError {
                code: string(code.as_str(), owners)?,
                message: string(message.as_str(), owners)?,
                details: details
                    .map(|value| string(value.as_str(), owners))
                    .transpose()?,
            }))
        }
        _ => bail!("native DATA result payload differs"),
    }
}
