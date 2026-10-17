//! Generic Core effect ABI values. All guest-shaped copies use the actual
//! invoking provider's payload budget; fixed outer result arity is Core control.
use crate::owned_values::ValueOwners;
use anyhow::{Context, Result, bail};
use std::fmt::Write;
use tobkiri_canon::{
    AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreValue, Digest,
    MAX_SAFE_INTEGER, ownership::NetworkDigestLengths,
};
use wasmtime::component::{BudgetedBox, BudgetedString, BudgetedVec, Val};

pub(crate) struct EffectValues<'a>(pub(crate) &'a ValueOwners);

/// Borrowed, shape-checked inputs retain their original physical owners. The
/// only operation offered here is the pure digest, with a finite new owner.
pub(crate) struct NetworkDigestInput<'a> {
    request: &'a Val,
    names: &'a [String],
    body: &'a [Val],
    bound: AllocationRequest,
}
impl NetworkDigestInput<'_> {
    pub(crate) fn bound(&self) -> AllocationRequest {
        self.bound
    }
    pub(crate) fn digest(&self, owners: &ValueOwners) -> Result<Digest> {
        // The fixed shape was checked while collecting lengths. Only the u8
        // body walk remains, and its complete work precedes its first element.
        owners.graph().reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: u64::try_from(self.body.len())?,
            depth: 3,
            work: u64::try_from(self.body.len())?,
        })?;
        if self.body.iter().any(|value| !matches!(value, Val::U8(_))) {
            bail!("network digest body must be list<u8>")
        }
        let names = CoreValue::array_from(
            self.names
                .iter()
                .map(|name| CoreValue::string(name, owners.graph())),
            owners.graph(),
        )?;
        let request = EffectValues(owners).wire(self.request)?;
        Ok(CoreValue::object_shared(
            [Ok(("request", request)), Ok(("secret-names", names))].into_iter(),
            owners.graph(),
        )?
        .digest_guest(owners.graph())?)
    }
}

// This bounded field-name walk is used only after its metadata work reservation.
// An overlong/unknown name is rejected before comparing any unbounded string.
fn network_fields<'a, const N: usize>(
    value: &'a Val,
    expected: [&str; N],
    maximum_name: usize,
) -> Result<[&'a Val; N]> {
    let Val::Record(fields) = value else {
        bail!("network digest requires a record")
    };
    if fields.len() != N {
        bail!("network digest record arity changed")
    }
    let mut values = [value; N];
    let mut seen = [false; N];
    for (name, child) in fields.iter() {
        if name.len() > maximum_name {
            bail!("network digest field name exceeds its shape")
        }
        let index = expected
            .iter()
            .position(|expected| *expected == name.as_str())
            .context("network digest has an unknown field")?;
        if seen[index] {
            bail!("network digest has a duplicate field")
        }
        seen[index] = true;
        values[index] = child;
    }
    Ok(values)
}

impl EffectValues<'_> {
    pub(crate) fn prepare_network_digest<'a>(
        &self,
        request: &'a Val,
        names: &'a [String],
    ) -> Result<NetworkDigestInput<'a>> {
        // Even length collection and fixed-name validation are paid by the
        // original graph. Neither scan can mint conversion work or authority.
        self.0.graph().reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 6,
            depth: 4,
            work: 1024,
        })?;
        let [method, url, headers, descriptors, body, timeout] = network_fields(
            request,
            [
                "method",
                "url",
                "headers",
                "secret-headers",
                "body",
                "timeout-ms",
            ],
            14,
        )?;
        let headers = list(headers)?;
        let descriptors = list(descriptors)?;
        if headers
            .len()
            .checked_add(descriptors.len())
            .context("network digest header count overflow")?
            > 128
            || names.len() > 32
        {
            bail!("network digest metadata count exceeds its bound")
        }
        let body = match body {
            Val::Option(None) => &[][..],
            Val::Option(Some(value)) => list(value)?,
            _ => bail!("network digest body must be option<list<u8>>"),
        };
        match timeout {
            Val::Option(None) => (),
            Val::Option(Some(value)) if matches!(value.as_ref(), Val::U32(_)) => (),
            _ => bail!("network digest timeout must be option<u32>"),
        }
        let mut lengths = NetworkDigestLengths {
            body_bytes: body.len(),
            headers: headers.len(),
            secret_headers: descriptors.len(),
            secret_names: names.len(),
            method_url_bytes: text(method)?
                .len()
                .checked_add(text(url)?.len())
                .context("network digest target length overflow")?,
            ..NetworkDigestLengths::default()
        };
        // Counts are capped above. Per-header tuple work and per-descriptor
        // three-name comparisons have fixed bounds; string bytes are not read.
        let metadata_work = headers.len() * 16 + descriptors.len() * 256 + names.len() * 2;
        self.0.graph().reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: u64::try_from(headers.len() + descriptors.len() + names.len())?,
            depth: 4,
            work: u64::try_from(metadata_work)?,
        })?;
        for header in headers {
            let Val::Tuple(pair) = header else {
                bail!("network digest header must be a tuple")
            };
            if pair.len() != 2 {
                bail!("network digest header tuple arity changed")
            }
            lengths.header_bytes = lengths
                .header_bytes
                .checked_add(text(&pair[0])?.len())
                .and_then(|n| n.checked_add(text(&pair[1]).ok()?.len()))
                .context("network digest header type or length is invalid")?;
        }
        for descriptor in descriptors {
            let [name, prefix, index] = network_fields(descriptor, ["name", "prefix", "index"], 6)?;
            if !matches!(index, Val::U32(_)) {
                bail!("network digest secret index must be u32")
            }
            lengths.secret_header_bytes = lengths
                .secret_header_bytes
                .checked_add(text(name)?.len())
                .and_then(|n| n.checked_add(text(prefix).ok()?.len()))
                .context("network digest secret header type or length is invalid")?;
        }
        for name in names {
            lengths.secret_name_bytes = lengths
                .secret_name_bytes
                .checked_add(name.len())
                .context("network digest secret-name length overflow")?;
        }
        Ok(NetworkDigestInput {
            request,
            names,
            body,
            bound: CoreValue::network_digest_conversion_bound(lengths)?,
        })
    }

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

#[cfg(test)]
pub(crate) fn network_digest_fixture(
    owners: &ValueOwners,
    body: Option<usize>,
    headers: usize,
    descriptors: usize,
    names: usize,
) -> (Val, Vec<String>) {
    let codec = EffectValues(owners);
    let headers = (0..headers)
        .map(|index| {
            codec
                .tuple(
                    [
                        codec.string(&format!("x-fixture-{index}")).unwrap(),
                        codec.string("quoted\"\\\n\u{1}日本語").unwrap(),
                    ]
                    .into_iter(),
                )
                .unwrap()
        })
        .collect::<Vec<_>>();
    let descriptors = (0..descriptors)
        .map(|_| {
            codec
                .record(
                    [
                        ("name", codec.string("authorization").unwrap()),
                        ("prefix", codec.string("Bearer ").unwrap()),
                        ("index", Val::U32(0)),
                    ]
                    .into_iter(),
                )
                .unwrap()
        })
        .collect::<Vec<_>>();
    let body = body
        .map(|length| {
            codec
                .some(
                    codec
                        .bytes(&(0..length).map(|index| index as u8).collect::<Vec<_>>())
                        .unwrap(),
                )
                .unwrap()
        })
        .unwrap_or(Val::Option(None));
    let request = codec
        .record(
            [
                ("method", codec.string("POST").unwrap()),
                (
                    "url",
                    codec
                        .string("https://example.invalid/v1/chat/completions")
                        .unwrap(),
                ),
                ("headers", codec.list(headers.into_iter()).unwrap()),
                (
                    "secret-headers",
                    codec.list(descriptors.into_iter()).unwrap(),
                ),
                ("body", body),
                ("timeout-ms", codec.some(Val::U32(u32::MAX)).unwrap()),
            ]
            .into_iter(),
        )
        .unwrap();
    (
        request,
        (0..names)
            .map(|index| format!("provider-fixture-{index}"))
            .collect(),
    )
}

#[cfg(test)]
pub(crate) fn baseline_network_digest(
    request: &Val,
    names: &[String],
    owners: &ValueOwners,
) -> Digest {
    // The pre-change implementation is intentionally kept as a differential
    // reference; it does not call the new preparation/digest helper.
    let names = CoreValue::array_from(
        names
            .iter()
            .map(|name| CoreValue::string(name, owners.graph())),
        owners.graph(),
    )
    .unwrap();
    let request = EffectValues(owners).wire(request).unwrap();
    CoreValue::object_shared(
        [Ok(("request", request)), Ok(("secret-names", names))].into_iter(),
        owners.graph(),
    )
    .unwrap()
    .digest_guest(owners.graph())
    .unwrap()
}

#[cfg(test)]
mod network_digest_tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use tobkiri_canon::AllocationBudget;
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

    #[derive(Default)]
    struct Usage {
        calls: usize,
        live: u64,
        peak: u64,
        work: u64,
    }
    struct Budget {
        bytes: u64,
        work: u64,
        depth: u32,
        usage: Mutex<Usage>,
    }
    impl Budget {
        fn new(bytes: u64, work: u64, depth: u32) -> Arc<Self> {
            Arc::new(Self {
                bytes,
                work,
                depth,
                usage: Mutex::default(),
            })
        }
        fn owners(self: &Arc<Self>) -> ValueOwners {
            ValueOwners::from_budgets(self.clone(), self.clone())
        }
    }
    impl AllocationBudget for Budget {
        fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
            let mut usage = self.usage.lock().unwrap();
            usage.calls += 1;
            if request.depth > self.depth {
                return Err(AllocationError::Depth);
            }
            let live = usage
                .live
                .checked_add(request.bytes)
                .ok_or(AllocationError::Overflow)?;
            let work = usage
                .work
                .checked_add(request.work.max(request.elements))
                .ok_or(AllocationError::Overflow)?;
            if live > self.bytes || work > self.work {
                return Err(AllocationError::Limit);
            }
            usage.live = live;
            usage.peak = usage.peak.max(live);
            usage.work = work;
            Ok(())
        }
        fn release(&self, bytes: u64) {
            let mut usage = self.usage.lock().unwrap();
            usage.live = usage
                .live
                .checked_sub(bytes)
                .expect("physical release must be charged");
        }
    }
    impl HostAllocationBudget for Budget {
        fn reserve(
            &self,
            request: HostAllocationRequest,
        ) -> std::result::Result<(), HostAllocationError> {
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
            AllocationBudget::release(self, bytes)
        }
    }

    #[test]
    fn network_digest_matches_old_hash_and_measures_five_admissions_per_body_byte() {
        let mut empty_calls = None;
        for length in [0usize, 1, 256, 1024, 6144] {
            let source = Budget::new(u64::MAX, u64::MAX, 64);
            let owners = source.owners();
            let (request, names) = network_digest_fixture(&owners, Some(length), 2, 1, 2);
            let before = source.usage.lock().unwrap().calls;
            let expected = baseline_network_digest(&request, &names, &owners);
            let calls = source.usage.lock().unwrap().calls - before;
            let fixed = *empty_calls.get_or_insert(calls);
            assert_eq!(calls, fixed + 5 * length);
            let prepared = EffectValues(&owners)
                .prepare_network_digest(&request, &names)
                .unwrap();
            let bound = prepared.bound();
            let counted = Budget::new(bound.bytes, bound.work, bound.depth);
            let actual = prepared.digest(&counted.owners()).unwrap();
            assert_eq!(actual, expected);
            let usage = counted.usage.lock().unwrap();
            assert_eq!(usage.live, 0);
            assert!(usage.peak <= bound.bytes);
            assert!(usage.work <= bound.work);
            drop(usage);
            drop(request);
            drop(owners);
            assert_eq!(source.usage.lock().unwrap().live, 0);
        }
    }

    #[test]
    fn network_digest_bound_covers_metadata_limits_and_null_body_without_changing_hash() {
        for (body, h, d, s) in [
            (None, 0, 0, 0),
            (Some(0), 128, 0, 32),
            (Some(1024), 64, 64, 32),
            (Some(6144), 0, 128, 32),
        ] {
            let source = Budget::new(u64::MAX, u64::MAX, 64);
            let owners = source.owners();
            let (request, names) = network_digest_fixture(&owners, body, h, d, s);
            let expected = baseline_network_digest(&request, &names, &owners);
            let prepared = EffectValues(&owners)
                .prepare_network_digest(&request, &names)
                .unwrap();
            let bound = prepared.bound();
            let bounded = Budget::new(bound.bytes, bound.work, 4);
            assert_eq!(prepared.digest(&bounded.owners()).unwrap(), expected);
            assert_eq!(bounded.usage.lock().unwrap().live, 0);
        }
    }

    #[test]
    fn network_digest_preparation_and_conversion_reject_unfunded_work_bytes_and_depth() {
        let source = Budget::new(u64::MAX, u64::MAX, 64);
        let owners = source.owners();
        let (request, names) = network_digest_fixture(&owners, Some(1024), 2, 1, 2);
        for (work, depth) in [(0, 64), (1024, 3), (1024, 64)] {
            let denied = Budget::new(u64::MAX, work, depth);
            assert!(
                EffectValues(&denied.owners())
                    .prepare_network_digest(&request, &names)
                    .is_err()
            );
            assert_eq!(denied.usage.lock().unwrap().live, 0);
        }
        let prepared = EffectValues(&owners)
            .prepare_network_digest(&request, &names)
            .unwrap();
        let bound = prepared.bound();
        for denied in [
            Budget::new(0, bound.work, 4),
            Budget::new(bound.bytes, 1023, 4),
            Budget::new(bound.bytes, bound.work, 2),
        ] {
            assert!(prepared.digest(&denied.owners()).is_err());
            assert_eq!(denied.usage.lock().unwrap().live, 0);
        }
        // The fixed shape is checked before the bounded codec can be entered.
        let Val::Record(fields) = &request else {
            unreachable!()
        };
        let bad = EffectValues(&owners)
            .record(fields.iter().map(|(key, value)| {
                (
                    key.as_str(),
                    if key.as_str() == "timeout-ms" {
                        Val::U32(1)
                    } else {
                        value.clone()
                    },
                )
            }))
            .unwrap();
        assert!(
            EffectValues(&owners)
                .prepare_network_digest(&bad, &names)
                .is_err()
        );
        let invalid_byte = EffectValues(&owners)
            .some(
                EffectValues(&owners)
                    .list([Val::U16(256)].into_iter())
                    .unwrap(),
            )
            .unwrap();
        let bad = EffectValues(&owners)
            .record(fields.iter().map(|(key, value)| {
                (
                    key.as_str(),
                    if key.as_str() == "body" {
                        invalid_byte.clone()
                    } else {
                        value.clone()
                    },
                )
            }))
            .unwrap();
        let prepared = EffectValues(&owners)
            .prepare_network_digest(&bad, &names)
            .unwrap();
        let bounded = Budget::new(prepared.bound().bytes, prepared.bound().work, 4);
        assert!(prepared.digest(&bounded.owners()).is_err());
        let usage = bounded.usage.lock().unwrap();
        assert_eq!(usage.calls, 1);
        assert_eq!(usage.peak, 0);
    }
}
