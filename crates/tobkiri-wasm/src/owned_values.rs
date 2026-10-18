//! Dynamic component/opaque graph conversions. No guest String/Vec is extracted
//! into an unleased value, including temporary formatting and base64 buffers.
use crate::HandleKind;
use anyhow::{Context, Result, bail};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use std::{fmt::Write, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreRef,
    CoreValue,
};
use tobkiri_manifest::WitType;
use wasmtime::component::{
    BudgetedBox, BudgetedString, BudgetedVec, BudgetedVecBuilder, HostAllocationOwner, Val,
};

/// Sharing handles for the same physically funded graph/component owner.
/// Only the verified Wasm frame factory can construct this pair.
#[derive(Clone)]
pub struct ValueOwners {
    pub(crate) graph: Arc<dyn AllocationBudget>,
    pub(crate) component: HostAllocationOwner,
    pub(crate) allocation_failure: Option<Arc<std::sync::atomic::AtomicU8>>,
}
impl ValueOwners {
    /// Pair trusted Rust codec budgets. This conveys no execution permission,
    /// active-frame proof or Core funding authority. Production PackHost makes
    /// its pair only from Gate's actual producing-frame scope; external Rust
    /// embedders are responsible for the custody of the supplied budgets.
    pub fn from_budgets(graph: Arc<dyn AllocationBudget>, component: HostAllocationOwner) -> Self {
        Self {
            graph,
            component,
            allocation_failure: None,
        }
    }
    /// Borrow the immutable graph allocator; cloning it shares existing custody.
    pub fn graph(&self) -> &Arc<dyn AllocationBudget> {
        &self.graph
    }
    /// Borrow the component allocator for a physically funded future/container.
    pub fn component(&self) -> &HostAllocationOwner {
        &self.component
    }
    pub(crate) fn allocation_failure_code(&self) -> &'static str {
        use std::sync::atomic::Ordering;
        match self
            .allocation_failure
            .as_ref()
            .map(|state| state.load(Ordering::Acquire))
        {
            None => "external-owner",
            Some(0) => "none",
            Some(1) => "inherited-process",
            Some(2) => "state-poisoned",
            Some(3) => "invalid-accounting",
            Some(4) => "scope-denied",
            Some(5) => "scope-deadline",
            Some(6) => "scope-cancelled",
            Some(7) => "scope-persistence",
            Some(8) => "scope-unavailable",
            Some(9) => "scope-limit",
            Some(10) => "scope-other",
            Some(11) => "scope-no-admission",
            Some(12) => "refund-underflow",
            Some(13) => "refund-resize-failed",
            Some(_) => "invalid-diagnostic",
        }
    }
}
struct ScalarText {
    bytes: [u8; 80],
    length: usize,
}
impl ScalarText {
    fn new() -> Self {
        Self {
            bytes: [0; 80],
            length: 0,
        }
    }
    fn as_str(&self) -> &str {
        // Only UTF-8 Rust formatting enters this fixed region.
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
fn integer(value: &CoreValue) -> Result<i128> {
    match value.inspect() {
        CoreRef::Int(n) => Ok(i128::from(n)),
        CoreRef::Str(text) | CoreRef::BigInt(text) => {
            let number = text.parse::<i128>().context("invalid typed integer")?;
            let mut canonical = ScalarText::new();
            write!(&mut canonical, "{number}")?;
            if text != canonical.as_str() {
                bail!("noncanonical typed integer")
            }
            Ok(number)
        }
        _ => bail!("typed integer requires an integer or decimal string"),
    }
}
fn float32(value: &CoreValue) -> Result<f32> {
    let text = value
        .as_str()
        .context("typed f32 requires its canonical scientific string")?;
    let number = text.parse::<f32>().context("invalid f32")?;
    let mut canonical = ScalarText::new();
    write!(&mut canonical, "{number:e}")?;
    if !number.is_finite() || text != canonical.as_str() {
        bail!("noncanonical or nonfinite f32")
    }
    Ok(number)
}
fn float64(value: &CoreValue) -> Result<f64> {
    let text = value
        .as_str()
        .context("typed f64 requires its canonical scientific string")?;
    let number = text.parse::<f64>().context("invalid f64")?;
    let mut canonical = ScalarText::new();
    write!(&mut canonical, "{number:e}")?;
    if !number.is_finite() || text != canonical.as_str() {
        bail!("noncanonical or nonfinite f64")
    }
    Ok(number)
}
fn graph_error(_: anyhow::Error) -> AllocationError {
    AllocationError::Invalid
}
fn named(name: &str, owner: &HostAllocationOwner, depth: usize) -> Result<BudgetedString> {
    Ok(BudgetedString::copy_from(name, owner, depth)?)
}
fn type_width(ty: &WitType) -> usize {
    match ty {
        WitType::Record(fields) => fields.len(),
        WitType::Tuple(types) => types.len(),
        WitType::Variant(cases) => cases.len(),
        WitType::Enum(cases) | WitType::Flags(cases) => cases.len(),
        _ => 1,
    }
}
fn val_width(value: &Val) -> usize {
    match value {
        Val::List(values) | Val::Tuple(values) => values.len(),
        Val::Record(fields) => fields.len(),
        Val::Flags(flags) => flags.len(),
        Val::String(text) | Val::Enum(text) => text.as_str().len(),
        _ => 1,
    }
}
fn core_width(value: &CoreValue) -> usize {
    match value.inspect() {
        CoreRef::Arr(values) => values.len(),
        CoreRef::Obj(fields) => fields.len(),
        CoreRef::Str(text) | CoreRef::BigInt(text) => text.len(),
        _ => 1,
    }
}
fn precharge_traversal(
    owners: &ValueOwners,
    width: usize,
    shape_width: usize,
    depth: usize,
) -> Result<()> {
    // Names/flags use bounded linear searches. Account their worst-case
    // comparisons before the first search, including duplicated aliases.
    let work = width
        .max(1)
        .checked_mul(shape_width.max(1))
        .and_then(|work| work.checked_mul(2))
        .context("typed traversal work overflow")?;
    owners.graph.reserve(AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: 0,
        elements: u64::try_from(width)?,
        depth: u32::try_from(depth)?,
        work: u64::try_from(work)?,
    })?;
    Ok(())
}

/// Convert an opaque value under the explicit actual producing payload owner.
pub fn core_to_val(value: &CoreValue, ty: &WitType, owners: &ValueOwners) -> Result<Val> {
    to_val(value, ty, owners, 0)
}
fn to_val(value: &CoreValue, ty: &WitType, owners: &ValueOwners, depth: usize) -> Result<Val> {
    if depth > tobkiri_canon::MAX_DEPTH {
        bail!("typed value depth exceeded")
    }
    precharge_traversal(owners, core_width(value), type_width(ty), depth)?;
    let owner = &owners.component;
    macro_rules! int {
        ($variant:ident, $rust:ty) => {
            Val::$variant(<$rust>::try_from(integer(value)?).context("typed integer out of range")?)
        };
    }
    Ok(match ty {
        WitType::Bool => Val::Bool(value.as_bool().context("expected bool")?),
        WitType::U8 => int!(U8, u8),
        WitType::U16 => int!(U16, u16),
        WitType::U32 => int!(U32, u32),
        WitType::U64 => int!(U64, u64),
        WitType::S8 => int!(S8, i8),
        WitType::S16 => int!(S16, i16),
        WitType::S32 => int!(S32, i32),
        WitType::S64 => int!(S64, i64),
        WitType::F32 => Val::Float32(float32(value)?),
        WitType::F64 => Val::Float64(float64(value)?),
        WitType::Char => {
            let mut chars = value.as_str().context("expected char")?.chars();
            let first = chars.next().context("empty char")?;
            if chars.next().is_some() {
                bail!("char requires one Unicode scalar")
            }
            Val::Char(first)
        }
        WitType::String => Val::String(named(
            value.as_str().context("expected string")?,
            owner,
            depth,
        )?),
        WitType::Json => {
            let encoded = value.encode_guest(&owners.graph)?;
            Val::String(named(encoded.as_str()?, owner, depth)?)
        }
        WitType::List(inner) if **inner == WitType::U8 => {
            let text = value.as_str().context("expected base64url bytes")?;
            let tail = match text.len() % 4 {
                0 => 0,
                2 => 1,
                3 => 2,
                _ => bail!("invalid base64url length"),
            };
            let length = (text.len() / 4)
                .checked_mul(3)
                .and_then(|n| n.checked_add(tail))
                .context("base64 length overflow")?;
            let bytes = CoreBuffer::write_exact(length, &owners.graph, |out| {
                let written = URL_SAFE_NO_PAD
                    .decode_slice(text, out)
                    .map_err(|_| AllocationError::Invalid)?;
                if written != length {
                    return Err(AllocationError::Invalid);
                }
                Ok(())
            })?;
            Val::List(BudgetedVec::from_iter(
                bytes.as_bytes().iter().copied().map(Val::U8),
                owner,
                depth,
            )?)
        }
        WitType::List(inner) => {
            let array = value.array().context("typed list requires an array")?;
            let mut out = BudgetedVecBuilder::new(array.len(), owner, depth)?;
            for item in array.iter() {
                out.push(to_val(&item, inner, owners, depth + 1)?)?;
            }
            Val::List(out.finish()?)
        }
        WitType::Tuple(types) => {
            let array = value.array().context("typed tuple requires an array")?;
            if array.len() != types.len() {
                bail!("typed tuple has the wrong arity")
            }
            let mut out = BudgetedVecBuilder::new(array.len(), owner, depth)?;
            for (item, ty) in array.iter().zip(types) {
                out.push(to_val(&item, ty, owners, depth + 1)?)?;
            }
            Val::Tuple(out.finish()?)
        }
        WitType::Record(fields) => {
            let object = value.object().context("typed record requires an object")?;
            if object
                .iter()
                .any(|(key, _)| !fields.iter().any(|field| field.name == key))
            {
                bail!("unknown record field")
            }
            let mut out = BudgetedVecBuilder::new(fields.len(), owner, depth)?;
            for field in fields {
                let converted = match object.get(&field.name) {
                    Some(item) => to_val(&item, &field.ty, owners, depth + 1)?,
                    None if matches!(field.ty, WitType::Option(_)) => Val::Option(None),
                    None => bail!("required record field missing"),
                };
                out.push((named(&field.name, owner, depth)?, converted))?;
            }
            Val::Record(out.finish()?)
        }
        WitType::Variant(cases) => {
            let object = value.object().context("variant requires an object")?;
            let tag_value = object.get("tag").context("variant tag missing")?;
            let tag = tag_value.as_str().context("variant tag is not string")?;
            if object.iter().any(|(key, _)| key != "tag" && key != "val") {
                bail!("unknown variant field")
            }
            let case = cases
                .iter()
                .find(|case| case.name == tag)
                .context("unknown variant case")?;
            let payload = match &case.ty {
                Some(ty) => Some(BudgetedBox::new(
                    to_val(
                        &object.get("val").context("variant value missing")?,
                        ty,
                        owners,
                        depth + 1,
                    )?,
                    owner,
                    depth,
                )?),
                None => {
                    if object
                        .get("val")
                        .is_some_and(|v| !matches!(v.inspect(), CoreRef::Null))
                    {
                        bail!("unit variant has a payload")
                    }
                    None
                }
            };
            Val::Variant(named(tag, owner, depth)?, payload)
        }
        WitType::Enum(cases) => {
            let tag = value.as_str().context("enum requires a string")?;
            if !cases.iter().any(|case| case == tag) {
                bail!("unknown enum case")
            }
            Val::Enum(named(tag, owner, depth)?)
        }
        WitType::Flags(cases) => {
            let array = value.array().context("flags requires an array")?;
            if array.len() > cases.len() {
                bail!("flags exceed the declared case count")
            }
            let mut out = BudgetedVecBuilder::new(array.len(), owner, depth)?;
            for (index, item) in array.iter().enumerate() {
                let tag = item.as_str().context("flag requires a string")?;
                if !cases.iter().any(|case| case == tag)
                    || array
                        .iter()
                        .take(index)
                        .any(|prior| prior.as_str() == Some(tag))
                {
                    bail!("unknown or duplicate flag")
                }
                out.push(named(tag, owner, depth)?)?;
            }
            Val::Flags(out.finish()?)
        }
        WitType::Option(inner) => {
            if matches!(value.inspect(), CoreRef::Null) {
                Val::Option(None)
            } else {
                Val::Option(Some(BudgetedBox::new(
                    to_val(value, inner, owners, depth + 1)?,
                    owner,
                    depth,
                )?))
            }
        }
        WitType::Result { ok, err } => {
            let object = value.object().context("result requires an object")?;
            if object.iter().any(|(key, _)| key != "tag" && key != "val") {
                bail!("unknown result field")
            }
            let tag_value = object.get("tag").context("result tag missing")?;
            let tag = tag_value.as_str().context("result tag is not string")?;
            let ty = match tag {
                "ok" => ok,
                "err" => err,
                _ => bail!("invalid result tag"),
            };
            let payload = match ty {
                Some(ty) => Some(BudgetedBox::new(
                    to_val(
                        &object.get("val").context("result payload missing")?,
                        ty,
                        owners,
                        depth + 1,
                    )?,
                    owner,
                    depth,
                )?),
                None => {
                    if object
                        .get("val")
                        .is_some_and(|v| !matches!(v.inspect(), CoreRef::Null))
                    {
                        bail!("unit result has a payload")
                    }
                    None
                }
            };
            if tag == "ok" {
                Val::Result(Ok(payload))
            } else {
                Val::Result(Err(payload))
            }
        }
        WitType::StreamHandle | WitType::CallbackHandle => {
            let object = value.object().context("core handle requires an object")?;
            if object.len() != 1 {
                bail!("core handle record has extra fields")
            }
            let id = u64::try_from(integer(
                &object.get("id").context("core handle ID missing")?,
            )?)
            .context("invalid handle ID")?;
            Val::Record(BudgetedVec::from_iter(
                [(named("id", owner, depth)?, Val::U64(id))].into_iter(),
                owner,
                depth,
            )?)
        }
    })
}

/// Validate and copy a typed result into an immutable owner-retaining graph.
pub fn val_to_core(value: &Val, ty: &WitType, owners: &ValueOwners) -> Result<CoreValue> {
    from_val(value, ty, owners, 0)
}
fn from_val(value: &Val, ty: &WitType, owners: &ValueOwners, depth: usize) -> Result<CoreValue> {
    if depth > tobkiri_canon::MAX_DEPTH {
        bail!("typed value depth exceeded")
    }
    precharge_traversal(owners, val_width(value), type_width(ty), depth)?;
    let budget = &owners.graph;
    fn signed(number: i64, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue> {
        if number.unsigned_abs() <= tobkiri_canon::MAX_SAFE_INTEGER {
            return Ok(CoreValue::int(number, budget)?);
        }
        let mut text = ScalarText::new();
        write!(&mut text, "{number}")?;
        Ok(CoreValue::string(text.as_str(), budget)?)
    }
    fn unsigned(number: u64, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue> {
        if number <= tobkiri_canon::MAX_SAFE_INTEGER {
            return Ok(CoreValue::int(number as i64, budget)?);
        }
        let mut text = ScalarText::new();
        write!(&mut text, "{number}")?;
        Ok(CoreValue::string(text.as_str(), budget)?)
    }
    Ok(match (value, ty) {
        (Val::Bool(v), WitType::Bool) => CoreValue::bool(*v, budget)?,
        (Val::U8(v), WitType::U8) => unsigned((*v).into(), budget)?,
        (Val::U16(v), WitType::U16) => unsigned((*v).into(), budget)?,
        (Val::U32(v), WitType::U32) => unsigned((*v).into(), budget)?,
        (Val::U64(v), WitType::U64) => unsigned(*v, budget)?,
        (Val::S8(v), WitType::S8) => signed((*v).into(), budget)?,
        (Val::S16(v), WitType::S16) => signed((*v).into(), budget)?,
        (Val::S32(v), WitType::S32) => signed((*v).into(), budget)?,
        (Val::S64(v), WitType::S64) => signed(*v, budget)?,
        (Val::Float32(v), WitType::F32) => {
            if !v.is_finite() {
                bail!("nonfinite f32")
            }
            let mut text = ScalarText::new();
            write!(&mut text, "{v:e}")?;
            CoreValue::string(text.as_str(), budget)?
        }
        (Val::Float64(v), WitType::F64) => {
            if !v.is_finite() {
                bail!("nonfinite f64")
            }
            let mut text = ScalarText::new();
            write!(&mut text, "{v:e}")?;
            CoreValue::string(text.as_str(), budget)?
        }
        (Val::Char(v), WitType::Char) => {
            let mut bytes = [0; 4];
            CoreValue::string(v.encode_utf8(&mut bytes), budget)?
        }
        (Val::String(v), WitType::String) => CoreValue::string(v.as_str(), budget)?,
        (Val::String(v), WitType::Json) => CoreValue::parse_guest(v.as_str().as_bytes(), budget)?,
        (Val::List(items), WitType::List(ty)) if **ty == WitType::U8 => {
            let bytes = CoreBuffer::write_exact(items.len(), budget, |out| {
                for (item, target) in items.iter().zip(out) {
                    let Val::U8(byte) = item else {
                        return Err(AllocationError::Invalid);
                    };
                    *target = *byte;
                }
                Ok(())
            })?;
            let length =
                base64::encoded_len(bytes.len(), false).context("base64 length overflow")?;
            let encoded = CoreBuffer::write_exact(length, budget, |out| {
                if URL_SAFE_NO_PAD
                    .encode_slice(bytes.as_bytes(), out)
                    .map_err(|_| AllocationError::Invalid)?
                    != length
                {
                    return Err(AllocationError::Invalid);
                }
                Ok(())
            })?;
            CoreValue::string_from_utf8(&encoded, budget)?
        }
        (Val::List(items), WitType::List(ty)) => CoreValue::array_from(
            items
                .iter()
                .map(|item| from_val(item, ty, owners, depth + 1).map_err(graph_error)),
            budget,
        )?,
        (Val::Tuple(items), WitType::Tuple(types)) if items.len() == types.len() => {
            CoreValue::array_from(
                items
                    .iter()
                    .zip(types)
                    .map(|(item, ty)| from_val(item, ty, owners, depth + 1).map_err(graph_error)),
                budget,
            )?
        }
        (Val::Record(items), WitType::Record(fields)) if items.len() == fields.len() => {
            CoreValue::object_from(
                fields.iter().map(|field| {
                    let mut found = items.iter().filter(|(name, _)| name.as_str() == field.name);
                    let item = found.next().ok_or(AllocationError::Invalid)?;
                    if found.next().is_some() {
                        return Err(AllocationError::Invalid);
                    }
                    Ok((
                        field.name.as_str(),
                        from_val(&item.1, &field.ty, owners, depth + 1).map_err(graph_error)?,
                    ))
                }),
                budget,
            )?
        }
        (Val::Variant(tag, payload), WitType::Variant(cases)) => {
            let ty = cases
                .iter()
                .find(|case| case.name == tag.as_str())
                .context("unknown variant case")?
                .ty
                .as_ref();
            tagged(
                tag.as_str(),
                payload.as_ref().map(|v| v.as_ref()),
                ty,
                owners,
                depth,
            )?
        }
        (Val::Enum(tag), WitType::Enum(cases)) if cases.iter().any(|case| case == tag.as_str()) => {
            CoreValue::string(tag.as_str(), budget)?
        }
        (Val::Flags(flags), WitType::Flags(cases)) => {
            if flags.len() > cases.len() {
                bail!("flags exceed the declared case count")
            }
            for (index, flag) in flags.iter().enumerate() {
                if !cases.iter().any(|case| case == flag.as_str())
                    || flags[..index].iter().any(|prior| prior == flag)
                {
                    bail!("unknown or duplicate flag")
                }
            }
            CoreValue::array_from(
                flags
                    .iter()
                    .map(|flag| CoreValue::string(flag.as_str(), budget)),
                budget,
            )?
        }
        (Val::Option(None), WitType::Option(_)) => CoreValue::null(budget)?,
        (Val::Option(Some(item)), WitType::Option(ty)) => from_val(item, ty, owners, depth + 1)?,
        (Val::Result(result), WitType::Result { ok, err }) => match result {
            Ok(payload) => tagged(
                "ok",
                payload.as_ref().map(|v| v.as_ref()),
                ok.as_deref(),
                owners,
                depth,
            )?,
            Err(payload) => tagged(
                "err",
                payload.as_ref().map(|v| v.as_ref()),
                err.as_deref(),
                owners,
                depth,
            )?,
        },
        (Val::Record(items), WitType::StreamHandle | WitType::CallbackHandle)
            if items.len() == 1 && items[0].0.as_str() == "id" =>
        {
            let Val::U64(id) = items[0].1 else {
                bail!("core handle ID type mismatch")
            };
            CoreValue::object_from([Ok(("id", unsigned(id, budget)?))].into_iter(), budget)?
        }
        _ => bail!("component value does not match the authoritative shape"),
    })
}
fn tagged(
    tag: &str,
    payload: Option<&Val>,
    ty: Option<&WitType>,
    owners: &ValueOwners,
    depth: usize,
) -> Result<CoreValue> {
    let tag = CoreValue::string(tag, &owners.graph)?;
    match (payload, ty) {
        (Some(value), Some(ty)) => Ok(CoreValue::object_from(
            [
                Ok(("tag", tag)),
                Ok(("val", from_val(value, ty, owners, depth + 1)?)),
            ]
            .into_iter(),
            &owners.graph,
        )?),
        (None, None) => Ok(CoreValue::object_from(
            [Ok(("tag", tag))].into_iter(),
            &owners.graph,
        )?),
        _ => bail!("variant/result payload mismatch"),
    }
}

/// Preserve primitive component bits; shared unchanged values retain their
/// original owner. New structural containers are charged to the producing call.
pub(crate) fn project_val_owned(
    value: &Val,
    from: &WitType,
    to: &WitType,
    owners: &ValueOwners,
) -> Result<Val> {
    // This also rejects malformed/nonfinite typed results. The validation graph
    // is itself prepaid and held through validation, never a plain Canon copy.
    val_to_core(value, from, owners)?;
    project(value, from, to, owners, 0)
}
/// Return the projected component value and its validated immutable graph.
/// Equal authoritative types need one graph construction: projection still
/// performs its existing traversal charge and current producing-frame admission.
/// Different types retain the original validation/projection/reparse sequence.
pub(crate) fn project_val_and_core_owned(
    value: &Val,
    from: &WitType,
    to: &WitType,
    owners: &ValueOwners,
) -> Result<(Val, CoreValue)> {
    // These are immutable compiled shape descriptions, not guest value data.
    if from == to {
        let graph = val_to_core(value, from, owners)?;
        let projected = project(value, from, to, owners, 0)?;
        // Preserve the old second conversion's root work charge and fresh
        // current-frame admission before returning the validated graph.
        precharge_traversal(owners, val_width(&projected), type_width(to), 0)?;
        return Ok((projected, graph));
    }
    let projected = project_val_owned(value, from, to, owners)?;
    let graph = val_to_core(&projected, to, owners)?;
    Ok((projected, graph))
}

fn project(
    value: &Val,
    from: &WitType,
    to: &WitType,
    owners: &ValueOwners,
    depth: usize,
) -> Result<Val> {
    if depth > tobkiri_canon::MAX_DEPTH {
        bail!("typed projection depth exceeded")
    }
    let shape_width = type_width(from)
        .checked_add(type_width(to))
        .context("typed projection width overflow")?;
    precharge_traversal(owners, val_width(value), shape_width, depth)?;
    if from == to {
        return Ok(value.clone());
    }
    let owner = &owners.component;
    Ok(match (value, from, to) {
        (Val::Record(values), WitType::Record(old), WitType::Record(new)) => {
            let common = old.len().min(new.len());
            if old[..common]
                .iter()
                .zip(&new[..common])
                .any(|(a, b)| a.name != b.name)
                || old[common..]
                    .iter()
                    .chain(&new[common..])
                    .any(|f| !matches!(f.ty, WitType::Option(_)))
            {
                bail!("record versions require an adapter")
            }
            let mut out = BudgetedVecBuilder::new(new.len(), owner, depth)?;
            for field in new {
                let item = match old.iter().position(|old| old.name == field.name) {
                    Some(index) => project(
                        &values
                            .iter()
                            .find(|(name, _)| name.as_str() == field.name)
                            .context("record field missing")?
                            .1,
                        &old[index].ty,
                        &field.ty,
                        owners,
                        depth + 1,
                    )?,
                    None => Val::Option(None),
                };
                out.push((named(&field.name, owner, depth)?, item))?;
            }
            Val::Record(out.finish()?)
        }
        (Val::List(values), WitType::List(old), WitType::List(new)) => {
            let mut out = BudgetedVecBuilder::new(values.len(), owner, depth)?;
            for item in values.iter() {
                out.push(project(item, old, new, owners, depth + 1)?)?;
            }
            Val::List(out.finish()?)
        }
        (Val::Tuple(values), WitType::Tuple(old), WitType::Tuple(new))
            if old.len() == new.len() =>
        {
            let mut out = BudgetedVecBuilder::new(values.len(), owner, depth)?;
            for (item, (old, new)) in values.iter().zip(old.iter().zip(new)) {
                out.push(project(item, old, new, owners, depth + 1)?)?;
            }
            Val::Tuple(out.finish()?)
        }
        (Val::Option(payload), WitType::Option(old), WitType::Option(new)) => Val::Option(
            payload
                .as_ref()
                .map(|item| {
                    Ok::<_, anyhow::Error>(BudgetedBox::new(
                        project(item, old, new, owners, depth + 1)?,
                        owner,
                        depth,
                    )?)
                })
                .transpose()?,
        ),
        (Val::Enum(tag), WitType::Enum(_), WitType::Enum(cases))
            if cases.iter().any(|case| case == tag.as_str()) =>
        {
            value.clone()
        }
        (Val::Variant(tag, payload), WitType::Variant(old), WitType::Variant(new)) => {
            let old = old
                .iter()
                .find(|case| case.name == tag.as_str())
                .context("unknown source variant")?;
            let new = new
                .iter()
                .find(|case| case.name == tag.as_str())
                .context("variant case requires an adapter")?;
            let payload = project_payload(
                payload.as_ref(),
                old.ty.as_ref(),
                new.ty.as_ref(),
                owners,
                depth,
            )?;
            Val::Variant(tag.clone(), payload)
        }
        (
            Val::Result(result),
            WitType::Result {
                ok: old_ok,
                err: old_err,
            },
            WitType::Result {
                ok: new_ok,
                err: new_err,
            },
        ) => match result {
            Ok(payload) => Val::Result(Ok(project_payload(
                payload.as_ref(),
                old_ok.as_deref(),
                new_ok.as_deref(),
                owners,
                depth,
            )?)),
            Err(payload) => Val::Result(Err(project_payload(
                payload.as_ref(),
                old_err.as_deref(),
                new_err.as_deref(),
                owners,
                depth,
            )?)),
        },
        _ => bail!("shape versions require an explicit adapter"),
    })
}
fn project_payload(
    payload: Option<&BudgetedBox<Val>>,
    old: Option<&WitType>,
    new: Option<&WitType>,
    owners: &ValueOwners,
    depth: usize,
) -> Result<Option<BudgetedBox<Val>>> {
    match (payload, old, new) {
        (Some(item), Some(old), Some(new)) => Ok(Some(BudgetedBox::new(
            project(item, old, new, owners, depth + 1)?,
            &owners.component,
            depth,
        )?)),
        (None, None, None) => Ok(None),
        _ => bail!("variant/result payload requires an adapter"),
    }
}

/// Rebuild immutable containers when mapping authoritative core handles. Raw
/// record layouts cannot invent handle types or gain mutable payload access.
pub(crate) fn rewrite_handles_owned(
    value: &Val,
    ty: &WitType,
    owners: &ValueOwners,
    remap: &mut impl FnMut(HandleKind, u64) -> Result<u64>,
) -> Result<Val> {
    rewrite(value, ty, owners, remap, 0)
}
fn rewrite(
    value: &Val,
    ty: &WitType,
    owners: &ValueOwners,
    remap: &mut impl FnMut(HandleKind, u64) -> Result<u64>,
    depth: usize,
) -> Result<Val> {
    if depth > tobkiri_canon::MAX_DEPTH {
        bail!("handle mapping depth exceeded")
    }
    precharge_traversal(owners, val_width(value), type_width(ty), depth)?;
    let owner = &owners.component;
    Ok(match (value, ty) {
        (Val::Record(fields), WitType::StreamHandle | WitType::CallbackHandle) => {
            if fields.len() != 1 || fields[0].0.as_str() != "id" {
                bail!("core handle ID record is invalid")
            }
            let Val::U64(id) = fields[0].1 else {
                bail!("core handle ID is invalid")
            };
            let kind = if *ty == WitType::StreamHandle {
                HandleKind::Stream
            } else {
                HandleKind::Callback
            };
            Val::Record(BudgetedVec::from_iter(
                [(named("id", owner, depth)?, Val::U64(remap(kind, id)?))].into_iter(),
                owner,
                depth,
            )?)
        }
        (Val::Record(values), WitType::Record(fields)) => {
            if values.len() != fields.len() {
                bail!("record arity mismatch")
            }
            let mut out = BudgetedVecBuilder::new(fields.len(), owner, depth)?;
            for field in fields {
                let item = &values
                    .iter()
                    .find(|(name, _)| name.as_str() == field.name)
                    .context("record field missing")?
                    .1;
                out.push((
                    named(&field.name, owner, depth)?,
                    rewrite(item, &field.ty, owners, remap, depth + 1)?,
                ))?;
            }
            Val::Record(out.finish()?)
        }
        (Val::List(values), WitType::List(inner)) => {
            let mut out = BudgetedVecBuilder::new(values.len(), owner, depth)?;
            for item in values.iter() {
                out.push(rewrite(item, inner, owners, remap, depth + 1)?)?;
            }
            Val::List(out.finish()?)
        }
        (Val::Tuple(values), WitType::Tuple(types)) if values.len() == types.len() => {
            let mut out = BudgetedVecBuilder::new(values.len(), owner, depth)?;
            for (item, ty) in values.iter().zip(types) {
                out.push(rewrite(item, ty, owners, remap, depth + 1)?)?;
            }
            Val::Tuple(out.finish()?)
        }
        (Val::Option(Some(item)), WitType::Option(inner)) => Val::Option(Some(BudgetedBox::new(
            rewrite(item, inner, owners, remap, depth + 1)?,
            owner,
            depth,
        )?)),
        (Val::Variant(tag, Some(item)), WitType::Variant(cases)) => {
            let inner = cases
                .iter()
                .find(|case| case.name == tag.as_str())
                .and_then(|case| case.ty.as_ref())
                .context("variant payload type missing")?;
            Val::Variant(
                tag.clone(),
                Some(BudgetedBox::new(
                    rewrite(item, inner, owners, remap, depth + 1)?,
                    owner,
                    depth,
                )?),
            )
        }
        (Val::Result(result), WitType::Result { ok, err }) => {
            let (payload, inner, success) = match result {
                Ok(item) => (item, ok, true),
                Err(item) => (item, err, false),
            };
            let payload = match (payload, inner) {
                (Some(item), Some(inner)) => Some(BudgetedBox::new(
                    rewrite(item, inner, owners, remap, depth + 1)?,
                    owner,
                    depth,
                )?),
                (None, None) => None,
                _ => bail!("result payload type mismatch"),
            };
            if success {
                Val::Result(Ok(payload))
            } else {
                Val::Result(Err(payload))
            }
        }
        _ => value.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};
    struct Meter {
        live: AtomicU64,
        calls: AtomicU64,
        deny_at: AtomicU64,
        work_limit: u64,
    }
    impl Meter {
        fn new(work_limit: u64) -> Arc<Self> {
            Arc::new(Self {
                live: AtomicU64::new(0),
                calls: AtomicU64::new(0),
                deny_at: AtomicU64::new(u64::MAX),
                work_limit,
            })
        }
    }
    impl AllocationBudget for Meter {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
            if call == self.deny_at.load(Ordering::SeqCst) {
                return Err(AllocationError::Unavailable);
            }
            if request.work > self.work_limit {
                return Err(AllocationError::Limit);
            }
            self.live
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                    n.checked_add(request.bytes)
                })
                .map_err(|_| AllocationError::Overflow)?;
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.live
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(bytes))
                .expect("release must follow actual charged storage");
        }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
            AllocationBudget::reserve(
                self,
                AllocationRequest {
                    kind: AllocationKind::Temporary,
                    bytes: request.bytes,
                    elements: request.elements,
                    depth: request.depth,
                    work: request.work,
                },
            )
            .map_err(|_| HostAllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            AllocationBudget::release(self, bytes);
        }
    }
    fn owners(meter: &Arc<Meter>) -> ValueOwners {
        ValueOwners {
            graph: meter.clone(),
            component: meter.clone(),
            allocation_failure: None,
        }
    }
    #[test]
    fn typed_string_shared_clone_keeps_actual_payload_charge() {
        let meter = Meter::new(u64::MAX);
        let owners = owners(&meter);
        let input = CoreValue::string("owned payload", &owners.graph).unwrap();
        let value = core_to_val(&input, &WitType::String, &owners).unwrap();
        drop(input);
        let bytes = meter.live.load(Ordering::SeqCst);
        assert!(bytes > 0);
        let shared = value.clone();
        drop(value);
        assert_eq!(meter.live.load(Ordering::SeqCst), bytes);
        drop(shared);
        assert_eq!(meter.live.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn typed_float_negative_zero_extremes_keep_bits_and_nonfinite_rejects() {
        let meter = Meter::new(u64::MAX);
        let owners = owners(&meter);
        for number in [-0.0f64, f64::MIN, f64::MAX, f64::MIN_POSITIVE] {
            let value = Val::Float64(number);
            let projected =
                project_val_owned(&value, &WitType::F64, &WitType::F64, &owners).unwrap();
            let Val::Float64(projected) = projected else {
                panic!("wrong scalar type")
            };
            assert_eq!(projected.to_bits(), number.to_bits());
            let graph = val_to_core(&value, &WitType::F64, &owners).unwrap();
            let restored = core_to_val(&graph, &WitType::F64, &owners).unwrap();
            let Val::Float64(restored) = restored else {
                panic!("wrong restored type")
            };
            assert_eq!(restored.to_bits(), number.to_bits());
        }
        assert!(val_to_core(&Val::Float64(f64::NAN), &WitType::F64, &owners).is_err());
        assert!(val_to_core(&Val::Float64(f64::INFINITY), &WitType::F64, &owners).is_err());
        assert_eq!(meter.live.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn bytes_use_exact_prepaid_temporary_and_return_opaque_graph() {
        let meter = Meter::new(u64::MAX);
        let owners = owners(&meter);
        let ty = WitType::List(Box::new(WitType::U8));
        for text in ["", "AA", "AAE", "AAH_", "AAH_AA"] {
            let input = CoreValue::string(text, &owners.graph).unwrap();
            let raw = core_to_val(&input, &ty, &owners).unwrap();
            let output = val_to_core(&raw, &ty, &owners).unwrap();
            assert_eq!(output.as_str(), Some(text));
            drop(input);
            drop(raw);
            drop(output);
            assert_eq!(meter.live.load(Ordering::SeqCst), 0);
        }
        let invalid = CoreValue::string("A", &owners.graph).unwrap();
        assert!(core_to_val(&invalid, &ty, &owners).is_err());
    }
    #[test]
    fn host_lookup_work_denies_before_allocating_destination_container() {
        let source_meter = Meter::new(u64::MAX);
        let source = owners(&source_meter);
        let first = CoreValue::int(1, &source.graph).unwrap();
        let second = CoreValue::int(2, &source.graph).unwrap();
        let input = CoreValue::object_shared(
            [Ok(("a", first)), Ok(("b", second))].into_iter(),
            &source.graph,
        )
        .unwrap();
        let destination = Meter::new(1);
        let owners = owners(&destination);
        let ty = WitType::Record(vec![
            tobkiri_manifest::WitField {
                name: "a".into(),
                ty: WitType::S32,
            },
            tobkiri_manifest::WitField {
                name: "b".into(),
                ty: WitType::S32,
            },
        ]);
        assert!(core_to_val(&input, &ty, &owners).is_err());
        assert_eq!(destination.live.load(Ordering::SeqCst), 0);
    }

    fn public_json_result(owners: &ValueOwners) -> (Val, WitType) {
        let json = r#"{"phase":"goal_intake","known":true,"usage":{"input":2022,"output":649},"ids":["gen-public","run-public"],"large":"18446744073709551615","text":"公開\n要約"}"#;
        let payload = Val::String(BudgetedString::copy_from(json, owners.component(), 0).unwrap());
        let value = Val::Result(Ok(Some(
            BudgetedBox::new(payload, owners.component(), 0).unwrap(),
        )));
        let ty = WitType::Result {
            ok: Some(Box::new(WitType::Json)),
            err: None,
        };
        (value, ty)
    }

    #[test]
    fn same_type_result_reuses_one_graph_with_identical_public_bytes() {
        let source_meter = Meter::new(u64::MAX);
        let source = owners(&source_meter);
        let (raw, ty) = public_json_result(&source);

        let validation_meter = Meter::new(u64::MAX);
        let validation = owners(&validation_meter);
        drop(val_to_core(&raw, &ty, &validation).unwrap());
        let validation_calls = validation_meter.calls.load(Ordering::SeqCst);
        assert!(validation_calls > 1);
        assert_eq!(validation_meter.live.load(Ordering::SeqCst), 0);

        let original_meter = Meter::new(u64::MAX);
        let original = owners(&original_meter);
        let old_projected = project_val_owned(&raw, &ty, &ty, &original).unwrap();
        let old_graph = val_to_core(&old_projected, &ty, &original).unwrap();
        let old_calls = original_meter.calls.load(Ordering::SeqCst);

        let reused_meter = Meter::new(u64::MAX);
        let reused = owners(&reused_meter);
        let (new_projected, new_graph) =
            project_val_and_core_owned(&raw, &ty, &ty, &reused).unwrap();
        let new_calls = reused_meter.calls.load(Ordering::SeqCst);
        assert_eq!(new_calls, validation_calls + 2);
        assert_eq!(old_calls, new_calls + validation_calls - 1);
        let old_encoded = old_graph.encode_guest(source.graph()).unwrap();
        let new_encoded = new_graph.encode_guest(source.graph()).unwrap();
        assert_eq!(new_encoded.as_bytes(), old_encoded.as_bytes());
        assert!(matches!(old_projected, Val::Result(Ok(Some(_)))));
        assert!(matches!(new_projected, Val::Result(Ok(Some(_)))));

        drop((old_graph, new_graph, old_projected, new_projected));
        assert_eq!(original_meter.live.load(Ordering::SeqCst), 0);
        assert_eq!(reused_meter.live.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn same_type_result_still_checks_projection_admission_after_validation() {
        let source_meter = Meter::new(u64::MAX);
        let source = owners(&source_meter);
        let (raw, ty) = public_json_result(&source);
        let probe_meter = Meter::new(u64::MAX);
        let probe = owners(&probe_meter);
        drop(val_to_core(&raw, &ty, &probe).unwrap());
        let validation_calls = probe_meter.calls.load(Ordering::SeqCst);

        for denied_call in [validation_calls + 1, validation_calls + 2] {
            let denied_meter = Meter::new(u64::MAX);
            denied_meter.deny_at.store(denied_call, Ordering::SeqCst);
            let denied = owners(&denied_meter);
            let error = project_val_and_core_owned(&raw, &ty, &ty, &denied)
                .err()
                .expect("both current-frame admissions remain required");
            assert!(matches!(
                error.downcast_ref::<AllocationError>(),
                Some(AllocationError::Unavailable)
            ));
            assert_eq!(denied_meter.calls.load(Ordering::SeqCst), denied_call);
            assert_eq!(denied_meter.live.load(Ordering::SeqCst), 0);
        }
    }

    #[test]
    fn same_type_result_rejects_malformed_json_nonfinite_and_missing_payload() {
        let source_meter = Meter::new(u64::MAX);
        let source = owners(&source_meter);
        let malformed = Val::String(
            BudgetedString::copy_from(r#"{"same":1,"same":2}"#, source.component(), 0).unwrap(),
        );
        let missing = WitType::Result {
            ok: Some(Box::new(WitType::U8)),
            err: None,
        };
        for (raw, ty) in [
            (malformed, WitType::Json),
            (Val::Float64(f64::NAN), WitType::F64),
            (Val::Result(Ok(None)), missing),
        ] {
            let meter = Meter::new(u64::MAX);
            let owners = owners(&meter);
            assert!(project_val_and_core_owned(&raw, &ty, &ty, &owners).is_err());
            assert_eq!(meter.live.load(Ordering::SeqCst), 0);
        }
    }

    #[test]
    fn different_type_result_keeps_original_projection_and_reparse_charges() {
        let source_meter = Meter::new(u64::MAX);
        let source = owners(&source_meter);
        let raw = Val::Record(
            BudgetedVec::from_iter(
                [(
                    BudgetedString::copy_from("value", source.component(), 0).unwrap(),
                    Val::S32(42),
                )]
                .into_iter(),
                source.component(),
                0,
            )
            .unwrap(),
        );
        let from = WitType::Record(vec![tobkiri_manifest::WitField {
            name: "value".into(),
            ty: WitType::S32,
        }]);
        let to = WitType::Record(vec![
            tobkiri_manifest::WitField {
                name: "value".into(),
                ty: WitType::S32,
            },
            tobkiri_manifest::WitField {
                name: "extra".into(),
                ty: WitType::Option(Box::new(WitType::String)),
            },
        ]);
        let old_meter = Meter::new(u64::MAX);
        let old = owners(&old_meter);
        let old_projected = project_val_owned(&raw, &from, &to, &old).unwrap();
        let old_graph = val_to_core(&old_projected, &to, &old).unwrap();
        let old_calls = old_meter.calls.load(Ordering::SeqCst);
        let new_meter = Meter::new(u64::MAX);
        let new = owners(&new_meter);
        let (new_projected, new_graph) =
            project_val_and_core_owned(&raw, &from, &to, &new).unwrap();
        assert_eq!(new_meter.calls.load(Ordering::SeqCst), old_calls);
        assert_eq!(
            new_meter.live.load(Ordering::SeqCst),
            old_meter.live.load(Ordering::SeqCst)
        );
        let old_encoded = old_graph.encode_guest(source.graph()).unwrap();
        let new_encoded = new_graph.encode_guest(source.graph()).unwrap();
        assert_eq!(new_encoded.as_bytes(), old_encoded.as_bytes());
        assert!(matches!(
            new_graph.get_key("extra").unwrap().inspect(),
            CoreRef::Null
        ));
        drop((old_graph, new_graph, old_projected, new_projected));
        assert_eq!(old_meter.live.load(Ordering::SeqCst), 0);
        assert_eq!(new_meter.live.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn same_type_result_retains_both_original_payload_and_graph_owners() {
        let source_meter = Meter::new(u64::MAX);
        let source_weak = Arc::downgrade(&source_meter);
        let source = owners(&source_meter);
        let (raw, ty) = public_json_result(&source);
        let graph_meter = Meter::new(u64::MAX);
        let graph_weak = Arc::downgrade(&graph_meter);
        let graph_owner = owners(&graph_meter);
        let (projected, graph) = project_val_and_core_owned(&raw, &ty, &ty, &graph_owner).unwrap();
        drop((raw, source, source_meter, graph_owner, graph_meter));
        assert!(source_weak.upgrade().is_some());
        assert!(graph_weak.upgrade().is_some());
        assert!(graph.get_key("val").unwrap().get_key("known").is_some());
        drop(projected);
        assert!(source_weak.upgrade().is_none());
        assert!(graph_weak.upgrade().is_some());
        drop(graph);
        assert!(graph_weak.upgrade().is_none());
    }

    #[test]
    fn same_type_result_preserves_exact_handle_shape_without_minting_authority() {
        let source_meter = Meter::new(u64::MAX);
        let source = owners(&source_meter);
        for ty in [WitType::StreamHandle, WitType::CallbackHandle] {
            let raw = Val::Record(
                BudgetedVec::from_iter(
                    [(
                        BudgetedString::copy_from("id", source.component(), 0).unwrap(),
                        Val::U64(u64::MAX),
                    )]
                    .into_iter(),
                    source.component(),
                    0,
                )
                .unwrap(),
            );
            let meter = Meter::new(u64::MAX);
            let owners = owners(&meter);
            let (projected, graph) = project_val_and_core_owned(&raw, &ty, &ty, &owners).unwrap();
            let Val::Record(fields) = &projected else {
                panic!("handle representation changed");
            };
            assert!(matches!(fields[0].1, Val::U64(u64::MAX)));
            assert_eq!(
                graph.get_key("id").unwrap().as_str(),
                Some("18446744073709551615")
            );
            drop((projected, graph));
            assert_eq!(meter.live.load(Ordering::SeqCst), 0);
            let malformed = Val::Record(
                BudgetedVec::from_iter(std::iter::empty(), source.component(), 0).unwrap(),
            );
            assert!(project_val_and_core_owned(&malformed, &ty, &ty, &owners).is_err());
            assert_eq!(meter.live.load(Ordering::SeqCst), 0);
        }
    }
}
