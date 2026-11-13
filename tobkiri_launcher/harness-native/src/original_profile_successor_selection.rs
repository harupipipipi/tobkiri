//! DATA arguments of the first reachable `_legacy_v4_successor` call.
use super::*;
use tobkiri_canon::CoreBufferBuilder;

#[cfg(test)]
#[path = "original_profile_successor_selection_tests.rs"]
mod tests;

/// Scan the strictly loaded registry while its definition lock is held by the caller.
pub(in super::super) fn select_successor<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<SuccessorSelection> {
    let doc = &state.doc;
    let mut meter = Meter { op, budget };
    meter.charge_work(1)?;
    let legacy = field(doc, doc.root, "legacy");
    let source = lookup(doc, legacy, "source_document", &mut meter)?;
    for &entry in array(doc, field(doc, doc.root, "profiles")).expect("validated profiles") {
        meter.charge_work(1)?;
        if boolean(doc, field(doc, entry, "tombstone")) == Some(true) {
            continue;
        }
        let Some(&legacy_id) = array(doc, field(doc, entry, "legacy_ids"))
            .expect("validated legacy IDs")
            .first()
        else {
            continue;
        };
        let latest = *array(doc, field(doc, entry, "revisions"))
            .expect("validated history")
            .last()
            .expect("nonempty history");
        let current = field(doc, latest, "profile");
        if doc.entries(current).is_none() {
            continue;
        }
        if let Some(version) = lookup(doc, current, "profile_api_version", &mut meter)? {
            const V4: &str = "io.tobkiri.profile.v4";
            const V5: &str = "io.tobkiri.profile.v5";
            match doc.node(version) {
                Node::Array(_) => return Err(unhashable("unhashable type: 'list'")),
                Node::Object(_) => return Err(unhashable("unhashable type: 'dict'")),
                Node::String(version) => {
                    meter.charge_work((V4.len() + V5.len()) as u64)?;
                    if version.is(V4) || version.is(V5) {
                        continue;
                    }
                }
                Node::Scalar(_) => {}
            }
        }
        let legacy_id = text(doc, legacy_id).expect("validated legacy ID");
        let (item, item_origin) = match legacy_source_profile(doc, source, legacy_id, &mut meter)? {
            Some(id) if truthy(doc.node(id)) => (id, SuccessorItemOrigin::LegacySource),
            _ => (current, SuccessorItemOrigin::CurrentRevision),
        };
        let source_path = lookup(doc, legacy, "source_path", &mut meter)?;
        let profile_id = field(doc, entry, "profile_id");
        let source_path = source_path
            .filter(|&id| truthy(doc.node(id)))
            .map(|id| python_str(doc, id, &mut meter))
            .transpose()?;
        meter.charge_work(0)?;
        return Ok(SuccessorSelection::BuilderEntry {
            item,
            profile_id,
            item_origin,
            source_path: SuccessorSourcePath {
                _rendered: source_path,
            },
        });
    }
    meter.charge_work(0)?;
    Ok(SuccessorSelection::Exhausted)
}

fn unhashable(message: &'static str) -> OriginalAuthorityError {
    raw("builtins.TypeError", message)
}

// The arena's object fields are a physical list. Meter their actual scan,
// including arbitrary source keys and the current profile's version lookup.
fn lookup<S: OriginalAuthorityOwner>(
    doc: &Document,
    object: usize,
    name: &str,
    meter: &mut Meter<'_, '_, S>,
) -> Result<Option<usize>> {
    if let Some(fields) = doc.entries(object) {
        for (key, value) in fields {
            meter.charge_work(1 + key.0.len().min(name.len()) as u64)?;
            if key.is(name) {
                return Ok(Some(*value));
            }
        }
    }
    Ok(None)
}

fn equal<S: OriginalAuthorityOwner>(
    left: &[u32],
    right: &[u32],
    meter: &mut Meter<'_, '_, S>,
) -> Result<bool> {
    meter.charge_work(
        1 + if left.len() == right.len() {
            left.len() as u64
        } else {
            0
        },
    )?;
    Ok(left == right)
}

fn legacy_source_profile<S: OriginalAuthorityOwner>(
    doc: &Document,
    source: Option<usize>,
    legacy_id: &Text,
    meter: &mut Meter<'_, '_, S>,
) -> Result<Option<usize>> {
    let Some(source) = source else {
        return Ok(None);
    };
    let Some(profiles) = lookup(doc, source, "profiles", meter)? else {
        return Ok(None);
    };
    match doc.node(profiles) {
        Node::Object(profiles) => {
            for (key, item) in profiles {
                if equal(&key.0, &legacy_id.0, meter)? {
                    return Ok(doc.entries(*item).map(|_| *item));
                }
            }
        }
        Node::Array(profiles) => {
            for &item in profiles {
                meter.charge_work(1)?;
                if doc.entries(item).is_none() {
                    continue;
                }
                let id = match lookup(doc, item, "profile_id", meter)?
                    .filter(|&id| truthy(doc.node(id)))
                {
                    some @ Some(_) => some,
                    None => lookup(doc, item, "id", meter)?.filter(|&id| truthy(doc.node(id))),
                };
                let matches = match id {
                    Some(id) => match doc.node(id) {
                        Node::String(id) => equal(&id.0, &legacy_id.0, meter)?,
                        _ => {
                            let id = python_str(doc, id, meter)?;
                            meter
                                .charge_work(1 + id.len() as u64 * 2 + legacy_id.0.len() as u64)?;
                            let id = id.as_str().expect("strict registry str renders as UTF-8");
                            legacy_id.is(id)
                        }
                    },
                    None => legacy_id.0.is_empty(),
                };
                if matches {
                    return Ok(Some(item));
                }
            }
        }
        _ => {}
    }
    Ok(None)
}

fn python_str<S: OriginalAuthorityOwner>(
    doc: &Document,
    id: usize,
    meter: &mut Meter<'_, '_, S>,
) -> Result<CoreBuffer> {
    // All source text is strict JSON. Repr at most quadruples its input bytes:
    // a one-byte DEL can become a four-byte escape; separators grow 1->2.
    // Use the real input bound rather than the smaller canonical JSON limit.
    let max = doc.source_bytes().checked_mul(4).ok_or_else(capacity)?;
    let mut output = CoreBuffer::builder(meter.budget, max);
    if let Node::String(value) = doc.node(id) {
        // str(str) is unquoted, including spaces, control characters and Unicode.
        const CHUNK: usize = 256;
        let mut bytes = [0; 4 * CHUNK];
        for chunk in value.0.chunks(CHUNK) {
            meter.charge_work(chunk.len() as u64)?;
            let mut length = 0;
            for &point in chunk {
                let ch = char::from_u32(point).expect("strict registry Unicode");
                length += ch.encode_utf8(&mut bytes[length..]).len();
            }
            append(&mut output, &bytes[..length], meter)?;
        }
    } else {
        write_repr(doc, id, &mut output, meter)?;
    }
    meter.charge_work(0)?;
    output.finish().map_err(|_| capacity())
}

fn capacity() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn append<S: OriginalAuthorityOwner>(
    output: &mut CoreBufferBuilder,
    value: &[u8],
    meter: &mut Meter<'_, '_, S>,
) -> Result<()> {
    meter.charge_work(value.len() as u64)?;
    output.append(value).map_err(|_| capacity())
}

// Only the strictly validated arena reaches this traversal, so recursion is
// bounded by the original JSON depth. Dict repr uses input insertion order.
fn write_repr<S: OriginalAuthorityOwner>(
    doc: &Document,
    id: usize,
    output: &mut CoreBufferBuilder,
    meter: &mut Meter<'_, '_, S>,
) -> Result<()> {
    meter.charge_work(1)?;
    match doc.node(id) {
        Node::String(value) => {
            unicode15_1::write_python_repr(&value.0, output, meter).map_err(unicode_error)
        }
        Node::Scalar(CanonValue::Null) => append(output, b"None", meter),
        Node::Scalar(CanonValue::Bool(true)) => append(output, b"True", meter),
        Node::Scalar(CanonValue::Bool(false)) => append(output, b"False", meter),
        Node::Scalar(CanonValue::Int(value)) => {
            use std::io::Write;
            let mut bytes = [0; 20];
            let mut tail = &mut bytes[..];
            write!(tail, "{value}").expect("i64 decimal fits in 20 bytes");
            let len = 20 - tail.len();
            append(output, &bytes[..len], meter)
        }
        Node::Array(items) => {
            append(output, b"[", meter)?;
            for (index, &item) in items.iter().enumerate() {
                if index != 0 {
                    append(output, b", ", meter)?;
                }
                write_repr(doc, item, output, meter)?;
            }
            append(output, b"]", meter)
        }
        Node::Object(fields) => {
            append(output, b"{", meter)?;
            for (index, (key, item)) in fields.iter().enumerate() {
                if index != 0 {
                    append(output, b", ", meter)?;
                }
                unicode15_1::write_python_repr(&key.0, output, meter).map_err(unicode_error)?;
                append(output, b": ", meter)?;
                write_repr(doc, *item, output, meter)?;
            }
            append(output, b"}", meter)
        }
        Node::Scalar(_) => unreachable!("strict registry scalar"),
    }
}
