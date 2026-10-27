//! Generic Core effect ABI values. All guest-shaped copies use the actual
//! invoking provider's payload budget; fixed outer result arity is Core control.
use crate::owned_values::ValueOwners;
use anyhow::{Context, Result, bail};
use std::fmt::Write;
use tobkiri_canon::{AllocationError, CoreBuffer, CoreValue, Digest, MAX_SAFE_INTEGER};
use wasmtime::component::{BudgetedBox, BudgetedString, BudgetedVec, Val};

pub(crate) struct EffectValues<'a>(pub(crate) &'a ValueOwners);
impl EffectValues<'_> {
    pub(crate) fn string(&self, text: &str) -> Result<Val> {
        Ok(Val::String(BudgetedString::copy_from(
            text,
            self.0.component(),
            0,
        )?))
    }
    pub(crate) fn list<I>(&self, values: I) -> Result<Val>
    where
        I: ExactSizeIterator<Item = Val>,
    {
        Ok(Val::List(BudgetedVec::from_iter(
            values,
            self.0.component(),
            0,
        )?))
    }
    pub(crate) fn tuple<I>(&self, values: I) -> Result<Val>
    where
        I: ExactSizeIterator<Item = Val>,
    {
        Ok(Val::Tuple(BudgetedVec::from_iter(
            values,
            self.0.component(),
            0,
        )?))
    }
    pub(crate) fn record<'a, I>(&self, values: I) -> Result<Val>
    where
        I: ExactSizeIterator<Item = (&'a str, Val)>,
    {
        let mut fields =
            wasmtime::component::BudgetedVecBuilder::new(values.len(), self.0.component(), 0)?;
        for (name, value) in values {
            fields.push((
                BudgetedString::copy_from(name, self.0.component(), 0)?,
                value,
            ))?;
        }
        Ok(Val::Record(fields.finish()?))
    }
    pub(crate) fn bytes(&self, bytes: &[u8]) -> Result<Val> {
        self.list(bytes.iter().copied().map(Val::U8))
    }
    pub(crate) fn some(&self, value: Val) -> Result<Val> {
        Ok(Val::Option(Some(BudgetedBox::new(
            value,
            self.0.component(),
            0,
        )?)))
    }
    pub(crate) fn ok(&self, value: Option<Val>) -> Result<Vec<Val>> {
        Ok(vec![Val::Result(Ok(value
            .map(|value| BudgetedBox::new(value, self.0.component(), 0))
            .transpose()?))])
    }
    pub(crate) fn parse(&self, value: &Val) -> Result<CoreValue> {
        Ok(CoreValue::parse_guest(
            text(value)?.as_bytes(),
            self.0.graph(),
        )?)
    }
    pub(crate) fn json(&self, value: &CoreValue) -> Result<Val> {
        let buffer = value.encode_guest(self.0.graph())?;
        self.string(buffer.as_str()?)
    }
    pub(crate) fn copy_bytes(&self, value: &Val) -> Result<CoreBuffer> {
        let values = list(value)?;
        Ok(CoreBuffer::write_exact(
            values.len(),
            self.0.graph(),
            |out| {
                for (value, target) in values.iter().zip(out) {
                    let Val::U8(byte) = value else {
                        return Err(AllocationError::Invalid);
                    };
                    *target = *byte;
                }
                Ok(())
            },
        )?)
    }
    pub(crate) fn wire(&self, value: &Val) -> Result<CoreValue> {
        self.wire_depth(value, 0)
    }
    fn wire_depth(&self, value: &Val, depth: usize) -> Result<CoreValue> {
        if depth > tobkiri_canon::MAX_DEPTH {
            bail!("effect argument depth exceeded")
        }
        let budget = self.0.graph();
        let scalar = |number: i128| -> Result<CoreValue> {
            if number.unsigned_abs() <= u128::from(MAX_SAFE_INTEGER) {
                return Ok(CoreValue::int(number as i64, budget)?);
            }
            let mut text = ScalarText {
                bytes: [0; 80],
                length: 0,
            };
            write!(&mut text, "{number}")?;
            Ok(CoreValue::string(text.as_str(), budget)?)
        };
        Ok(match value {
            Val::String(text) | Val::Enum(text) => CoreValue::string(text.as_str(), budget)?,
            Val::Bool(value) => CoreValue::bool(*value, budget)?,
            Val::U8(value) => scalar((*value).into())?,
            Val::U16(value) => scalar((*value).into())?,
            Val::U32(value) => scalar((*value).into())?,
            Val::U64(value) => scalar((*value).into())?,
            Val::S8(value) => scalar((*value).into())?,
            Val::S16(value) => scalar((*value).into())?,
            Val::S32(value) => scalar((*value).into())?,
            Val::S64(value) => scalar((*value).into())?,
            Val::List(values) | Val::Tuple(values) => CoreValue::array_from(
                values.iter().map(|value| {
                    self.wire_depth(value, depth + 1)
                        .map_err(|_| AllocationError::Invalid)
                }),
                budget,
            )?,
            Val::Record(fields) => CoreValue::object_from(
                fields.iter().map(|(name, value)| {
                    Ok((
                        name.as_str(),
                        self.wire_depth(value, depth + 1)
                            .map_err(|_| AllocationError::Invalid)?,
                    ))
                }),
                budget,
            )?,
            Val::Option(None) => CoreValue::null(budget)?,
            Val::Option(Some(value)) => self.wire_depth(value, depth + 1)?,
            _ => bail!("effect argument has unsupported wire type"),
        })
    }
    pub(crate) fn args_hash(
        &self,
        interface: &str,
        version: &str,
        operation: &str,
        params: &[Val],
    ) -> Result<Digest> {
        let parameters = CoreValue::array_from(
            params
                .iter()
                .map(|value| self.wire(value).map_err(|_| AllocationError::Invalid)),
            self.0.graph(),
        )?;
        let envelope = CoreValue::object_shared(
            [
                Ok(("interface", CoreValue::string(interface, self.0.graph())?)),
                Ok(("version", CoreValue::string(version, self.0.graph())?)),
                Ok(("operation", CoreValue::string(operation, self.0.graph())?)),
                Ok(("params", parameters)),
            ]
            .into_iter(),
            self.0.graph(),
        )?;
        Ok(envelope.digest_guest(self.0.graph())?)
    }
}
pub(crate) fn text(value: &Val) -> Result<&str> {
    match value {
        Val::String(value) => Ok(value.as_str()),
        _ => bail!("expected string argument"),
    }
}
pub(crate) fn list(value: &Val) -> Result<&[Val]> {
    match value {
        Val::List(value) => Ok(value.as_slice()),
        _ => bail!("expected list argument"),
    }
}
pub(crate) fn field<'a>(value: &'a Val, name: &str) -> Result<&'a Val> {
    let Val::Record(fields) = value else {
        bail!("expected record argument")
    };
    fields
        .iter()
        .find(|(key, _)| key.as_str() == name)
        .map(|(_, value)| value)
        .context("required core argument field missing")
}
pub(crate) fn uint(value: &Val) -> Result<u64> {
    match value {
        Val::U64(value) => Ok(*value),
        Val::U32(value) => Ok(u64::from(*value)),
        Val::U16(value) => Ok(u64::from(*value)),
        _ => bail!("expected unsigned argument"),
    }
}
struct ScalarText {
    bytes: [u8; 80],
    length: usize,
}
impl ScalarText {
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.length]).expect("Rust scalar formatting is UTF-8")
    }
}
impl Write for ScalarText {
    fn write_str(&mut self, value: &str) -> std::fmt::Result {
        let end = self
            .length
            .checked_add(value.len())
            .ok_or(std::fmt::Error)?;
        if end > self.bytes.len() {
            return Err(std::fmt::Error);
        }
        self.bytes[self.length..end].copy_from_slice(value.as_bytes());
        self.length = end;
        Ok(())
    }
}
