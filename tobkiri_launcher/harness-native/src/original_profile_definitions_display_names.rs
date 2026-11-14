//! Preserve the legacy localized documents while appending runtime-facing revisions.
use super::*;

pub(in super::super) fn prepare_display_name_repair<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    budget: &Arc<dyn AllocationBudget>,
    clock: impl FnOnce() -> Result<i64>,
) -> Result<Option<(usize, CoreBuffer)>> {
    let doc = &state.doc;
    let entries = array(doc, field(doc, doc.root, "profiles")).expect("validated profiles");
    let mut funding = None;
    let mut repairs = Vec::new();
    for (position, &entry) in entries.iter().enumerate() {
        charge(op, budget, 1)?;
        if boolean(doc, field(doc, entry, "tombstone")) == Some(true) {
            continue;
        }
        let revisions = array(doc, field(doc, entry, "revisions")).expect("validated revisions");
        let profile = field(
            doc,
            *revisions.last().expect("nonempty revisions"),
            "profile",
        );
        if let Some(name) = repair_name(doc, profile, op, budget)? {
            if funding.is_none() {
                funding = Some(
                    op.reserve(
                        OriginalAuthorityStage::Source,
                        entries
                            .len()
                            .saturating_mul(std::mem::size_of::<(usize, &[u32])>()),
                    )?,
                );
                repairs
                    .try_reserve_exact(entries.len())
                    .map_err(|_| capacity(()))?;
                if repairs.capacity() != entries.len() {
                    return Err(capacity(()));
                }
            }
            repairs.push((position, name));
        }
    }
    if repairs.is_empty() {
        return Ok(None);
    }
    let now = clock()?;
    if now < 0 {
        return Err(raw(STORE_ERROR, "clock returned a negative timestamp"));
    }
    if now.unsigned_abs() > tobkiri_canon::MAX_SAFE_INTEGER {
        return Err(json::strict_error(
            "integer exceeds I-JSON safe range; use a canonical decimal string",
        ));
    }
    let timestamp = CoreValue::int(now, budget).map_err(capacity)?;
    let bytes = json::canonical(doc, None, op)?;
    let snapshot = CoreValue::parse_guest(&bytes, budget).map_err(capacity)?;
    drop(bytes);
    let profiles = snapshot.get_key("profiles").expect("validated profiles");
    let profiles = profiles.array().expect("validated profiles");
    let mut next = CoreValue::array_builder(budget, profiles.len()).map_err(capacity)?;
    let count = repairs.len();
    let mut repairs = repairs.into_iter().peekable();
    for (position, row) in profiles.iter().enumerate() {
        charge(op, budget, 1)?;
        let row = if repairs.peek().is_some_and(|(at, _)| *at == position) {
            let (_, name) = repairs.next().expect("selected repair");
            let history = row.get_key("revisions").expect("validated revisions");
            let history = history.array().expect("validated revisions");
            let old = history.get(history.len() - 1).expect("nonempty revisions");
            let profile = old.get_key("profile").expect("validated definition");
            let repaired = project_name(&profile, name, budget)?;
            append_successor(&row, &repaired, &timestamp, budget)?
        } else {
            row
        };
        next.push(row).map_err(capacity)?;
    }
    drop(repairs);
    drop(funding);
    let profiles = next.finish().map_err(capacity)?;
    let changed = i64::try_from(count).map_err(capacity)?;
    let output = encode_successors(&snapshot, profiles, &timestamp, changed, budget)?;
    Ok(Some((count, output)))
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn project_name(
    profile: &CoreValue,
    name: &[u32],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let fields = profile.object().expect("validated definition");
    let display = fields.get("display_name").expect("selected localized name");
    let mut bytes = CoreBuffer::builder(budget, name.len().saturating_mul(4));
    for &point in name {
        let mut storage = [0; 4];
        let point = char::from_u32(point).expect("strict registry Unicode");
        bytes
            .append(point.encode_utf8(&mut storage).as_bytes())
            .map_err(capacity)?;
    }
    let bytes = bytes.finish().map_err(capacity)?;
    let name = CoreValue::string_from_utf8(&bytes, budget).map_err(capacity)?;
    drop(bytes);
    let mut fields = fields.iter().map(|(key, value)| {
        Ok((
            key,
            match key {
                "display_name" => name.share(),
                "legacy_display_name" => display.share(),
                _ => value,
            },
        ))
    });
    // Preserve an existing field's position in the input sequence. The
    // canonical object builder sorts the final fields in either case.
    if profile.get_key("legacy_display_name").is_some() {
        return CoreValue::object_shared(fields, budget).map_err(capacity);
    }
    let size = fields.len() + 1;
    CoreValue::object_shared(
        (0..size).map(|_| {
            fields
                .next()
                .unwrap_or_else(|| Ok(("legacy_display_name", display.share())))
        }),
        budget,
    )
    .map_err(capacity)
}

fn repair_name<'doc, S: OriginalAuthorityOwner>(
    doc: &'doc Document,
    profile: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<&'doc [u32]>> {
    let fields = doc
        .entries(profile)
        .ok_or_else(|| integrity("live Profile revision document is invalid"))?;
    charge(op, budget, fields.len() as u64 * 4)?;
    let Some(display) = doc.get(profile, "display_name") else {
        return Ok(None);
    };
    let preserved = doc
        .get(profile, "legacy_display_name")
        .filter(|&id| !matches!(doc.node(id), Node::Scalar(CanonValue::Null)));
    if let Some(name) = text(doc, display) {
        charge(op, budget, name.0.len() as u64)?;
        if strip(&name.0).is_empty() {
            return Err(integrity("legacy Profile display_name is empty"));
        }
        if preserved.is_some_and(|id| doc.entries(id).is_none()) {
            return Err(integrity("legacy_display_name must preserve an object"));
        }
        return Ok(None);
    }
    let localized = doc
        .entries(display)
        .filter(|values| !values.is_empty())
        .ok_or_else(|| integrity("legacy Profile display_name is invalid"))?;
    if let Some(id) = preserved
        && !python_equal(doc, id, display, op, budget)?
    {
        return Err(integrity(
            "legacy Profile display_name preservation is ambiguous",
        ));
    }
    for (at, (key, name)) in localized.iter().enumerate() {
        charge(op, budget, 1 + key.0.len() as u64)?;
        let name = text(doc, *name)
            .ok_or_else(|| integrity("legacy Profile localized display_name is invalid"))?;
        charge(op, budget, name.0.len() as u64)?;
        if !valid_locale(&key.0) || strip(&name.0).is_empty() {
            return Err(integrity(
                "legacy Profile localized display_name is invalid",
            ));
        }
        for (prior, _) in &localized[..at] {
            charge(op, budget, 1 + key.0.len().min(prior.0.len()) as u64)?;
            if same_locale(&key.0, &prior.0) {
                return Err(integrity(
                    "legacy Profile localized display_name is ambiguous",
                ));
            }
        }
    }
    let locale = doc
        .get(profile, "locale")
        .filter(|&id| !matches!(doc.node(id), Node::Scalar(CanonValue::Null)));
    const JAPANESE: &[u32] = &[b'j' as u32, b'a' as u32];
    const ENGLISH: &[u32] = &[b'e' as u32, b'n' as u32];
    let mut candidates = [None, None, Some(JAPANESE), Some(ENGLISH)];
    if let Some(locale) = locale {
        let locale =
            text(doc, locale).ok_or_else(|| integrity("legacy Profile locale is invalid"))?;
        charge(op, budget, locale.0.len() as u64)?;
        if !valid_locale(&locale.0) {
            return Err(integrity("legacy Profile locale is invalid"));
        }
        candidates[0] = Some(&locale.0);
        candidates[1] = locale
            .0
            .iter()
            .position(|&ch| separator(ch))
            .map(|end| &locale.0[..end]);
    }
    for candidate in candidates.into_iter().flatten() {
        for (key, name) in localized {
            charge(op, budget, 1 + key.0.len().min(candidate.len()) as u64)?;
            if same_locale(&key.0, candidate) {
                let name = text(doc, *name).expect("validated localized name");
                charge(op, budget, name.0.len() as u64)?;
                return Ok(Some(strip(&name.0)));
            }
        }
    }
    if let Some(name) = doc.get(profile, "name").and_then(|id| text(doc, id)) {
        charge(op, budget, name.0.len() as u64)?;
        let name = strip(&name.0);
        if !name.is_empty() {
            return Ok(Some(name));
        }
    }
    Err(integrity(
        "legacy Profile localized display_name has no deterministic fallback",
    ))
}

fn strip(mut name: &[u32]) -> &[u32] {
    fn space(point: u32) -> bool {
        char::from_u32(point)
            .is_some_and(|ch| python_space(ch) || matches!(ch, '\u{1c}'..='\u{1f}'))
    }
    while name.first().copied().is_some_and(space) {
        name = &name[1..];
    }
    while name.last().copied().is_some_and(space) {
        name = &name[..name.len() - 1];
    }
    name
}

fn separator(point: u32) -> bool {
    point == u32::from(b'-') || point == u32::from(b'_')
}

fn valid_locale(points: &[u32]) -> bool {
    points
        .split(|&point| separator(point))
        .enumerate()
        .all(|(at, part)| {
            ((if at == 0 { 2 } else { 1 })..=8).contains(&part.len())
                && part.iter().all(|&point| {
                    u8::try_from(point).is_ok_and(|byte| {
                        byte.is_ascii_alphabetic() || (at > 0 && byte.is_ascii_digit())
                    })
                })
        })
}

fn same_locale(left: &[u32], right: &[u32]) -> bool {
    fn normalized(point: u32) -> u8 {
        if separator(point) {
            b'-'
        } else {
            (point as u8).to_ascii_lowercase()
        }
    }
    left.len() == right.len()
        && left
            .iter()
            .zip(right)
            .all(|(&a, &b)| normalized(a) == normalized(b))
}

// Registry loading validates strict JSON and its depth before this comparison.
// Python's comparison treats bool/int alike even inside malformed localized
// values. Preserve the original ordering of the comparison and validation.
fn python_equal<S: OriginalAuthorityOwner>(
    doc: &Document,
    left: usize,
    right: usize,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    charge(op, budget, 1)?;
    Ok(match (doc.node(left), doc.node(right)) {
        (Node::String(a), Node::String(b)) => {
            charge(op, budget, a.0.len().min(b.0.len()) as u64)?;
            a == b
        }
        (Node::Scalar(CanonValue::Int(a)), Node::Scalar(CanonValue::Bool(b)))
        | (Node::Scalar(CanonValue::Bool(b)), Node::Scalar(CanonValue::Int(a))) => {
            *a == i64::from(*b)
        }
        (Node::Scalar(a), Node::Scalar(b)) => a == b,
        (Node::Array(a), Node::Array(b)) if a.len() == b.len() => {
            for (&a, &b) in a.iter().zip(b) {
                if !python_equal(doc, a, b, op, budget)? {
                    return Ok(false);
                }
            }
            true
        }
        (Node::Object(a), Node::Object(b)) if a.len() == b.len() => {
            for (key, left) in a {
                let mut right = None;
                for (other, value) in b {
                    charge(op, budget, 1 + key.0.len().min(other.0.len()) as u64)?;
                    if other == key {
                        right = Some(*value);
                        break;
                    }
                }
                let Some(right) = right else { return Ok(false) };
                if !python_equal(doc, *left, right, op, budget)? {
                    return Ok(false);
                }
            }
            true
        }
        _ => false,
    })
}
