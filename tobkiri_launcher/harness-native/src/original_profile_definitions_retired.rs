//! Migrate only stored Profile intent. Pack-control authority is a separate operation.
use super::super::projection_io::{RETIREMENTS, Retirement};
use super::*;
use successor_selection::lookup;
use tobkiri_canon::{AllocationError, CoreArray, CoreRef};

const MIGRATION_ID: &str = "retired-declarative-packs-to-profile-projections-v1";

pub(in super::super) fn prepare_retired_pack_migration<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &State,
    budget: &Arc<dyn AllocationBudget>,
    clock: impl FnOnce() -> Result<i64>,
    mut inventory: impl FnMut(&Retirement) -> Result<(Digest, usize)>,
) -> Result<Option<(usize, CoreBuffer)>> {
    let doc = &state.doc;
    let entries = array(doc, field(doc, doc.root, "profiles")).expect("validated profiles");
    let mut snapshot = None;
    let mut prepared = Vec::new();
    let mut funding = None;
    let mut meter = Meter { op, budget };
    for (position, &entry) in entries.iter().enumerate() {
        charge(op, budget, 1)?;
        if boolean(doc, field(doc, entry, "tombstone")) == Some(true) {
            continue;
        }
        let history = array(doc, field(doc, entry, "revisions")).expect("validated history");
        let profile = field(doc, *history.last().expect("nonempty history"), "profile");
        let Some(packs) = lookup(doc, profile, "packs", &mut meter)?.and_then(|id| array(doc, id))
        else {
            continue;
        };
        let mut selected = [false; RETIREMENTS.len()];
        for &item in packs {
            charge(op, budget, 1)?;
            if let Some(retired) = retired_index(doc, item, &mut meter)? {
                selected[retired] = true;
            }
        }
        if selected.iter().all(|present| !present) {
            continue;
        }
        if snapshot.is_none() {
            let bytes = json::canonical(doc, None, op)?;
            snapshot = Some(CoreValue::parse_guest(&bytes, budget).map_err(capacity)?);
            let allocation = entries.len().saturating_mul(size_of::<Prepared>());
            funding = Some(op.reserve(OriginalAuthorityStage::Source, allocation)?);
            prepared
                .try_reserve_exact(entries.len())
                .map_err(capacity)?;
            if prepared.capacity() != entries.len() {
                return Err(capacity(()));
            }
        }
        let row = snapshot
            .as_ref()
            .and_then(|snapshot| snapshot.get_key("profiles"))
            .and_then(|profiles| profiles.get_index(position))
            .expect("validated row");
        let old = row.get_key("revisions").expect("validated history");
        let old = old
            .get_index(history.len() - 1)
            .expect("validated latest revision");
        let old_profile = old.get_key("profile").expect("validated profile");
        let migrated = migrate_document(
            doc,
            profile,
            &old_profile,
            selected,
            &mut inventory,
            &mut Meter { op, budget },
        )?;
        prepared.push(Prepared {
            position,
            selected,
            migrated,
        });
    }
    let Some(snapshot) = snapshot else {
        return Ok(None);
    };
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
    let profiles = snapshot.get_key("profiles").expect("validated profiles");
    let profiles = profiles.array().expect("validated list");
    let count = prepared.len();
    let mut next_profiles = CoreValue::array_builder(budget, profiles.len()).map_err(capacity)?;
    let mut receipts = CoreValue::array_builder(budget, count).map_err(capacity)?;
    let mut total = 0;
    let mut prepared = prepared.into_iter().peekable();
    for (position, row) in profiles.iter().enumerate() {
        charge(op, budget, 1)?;
        let row = if prepared
            .peek()
            .is_some_and(|item| item.position == position)
        {
            let item = prepared.next().expect("prepared migration");
            let successor = append_successor(&row, &item.migrated, &timestamp, budget)?;
            let migrated_count = item
                .selected
                .into_iter()
                .filter(|selected| *selected)
                .count();
            let mut ids = CoreValue::array_builder(budget, migrated_count).map_err(capacity)?;
            for (retirement, selected) in RETIREMENTS.iter().zip(item.selected) {
                if selected {
                    ids.push(string(retirement.pack, budget)?)
                        .map_err(capacity)?;
                }
            }
            total += migrated_count;
            let prior = row
                .get_key("current_revision")
                .expect("validated prior revision");
            let revision = successor.get_key("current_revision").expect("new revision");
            let receipt = object(
                [
                    ("migration_id", string(MIGRATION_ID, budget)?),
                    (
                        "profile_id",
                        row.get_key("profile_id").expect("validated id"),
                    ),
                    ("profile_revision", revision.share()),
                    ("prior_profile_revision", prior.share()),
                    ("migrated_pack_ids", ids.finish().map_err(capacity)?),
                    ("migrated_count", integer_value(migrated_count, budget)?),
                    ("pre_state_digest", prior.share()),
                    ("post_state_digest", revision),
                    ("rollback_digest", prior),
                ],
                budget,
            )?;
            receipts.push(receipt).map_err(capacity)?;
            successor
        } else {
            row
        };
        next_profiles.push(row).map_err(capacity)?;
    }
    drop(prepared);
    drop(funding);
    let record = object(
        [
            ("migration_id", string(MIGRATION_ID, budget)?),
            ("compatibility_release", string("v1.10.3", budget)?),
            ("remove_no_earlier_than_release", string("v1.10.4", budget)?),
            ("sunset_at", string("2026-10-31", budget)?),
            ("profile_count", integer_value(count, budget)?),
            ("migrated_count", integer_value(total, budget)?),
            ("receipts", receipts.finish().map_err(capacity)?),
        ],
        budget,
    )?;
    let legacy = snapshot
        .get_key("legacy")
        .expect("validated legacy metadata");
    let migrations = legacy.get_key("projection_migrations");
    let migrations = migrate_ledger(doc, migrations.as_ref(), record, op, budget)?;
    let legacy = update_object(&legacy, [("projection_migrations", migrations)], budget)?;
    let bootstrap = snapshot
        .get_key("bootstrap")
        .expect("validated bootstrap metadata");
    let output = encode_state(
        &snapshot,
        next_profiles.finish().map_err(capacity)?,
        &timestamp,
        i64::try_from(count).map_err(capacity)?,
        [legacy, bootstrap],
        budget,
    )?;
    Ok(Some((count, output)))
}

struct Prepared {
    position: usize,
    selected: [bool; RETIREMENTS.len()],
    migrated: CoreValue,
}

// A non-string JSON value cannot stringify to a fixed Pack ID beginning
// with "rumi_". Dict rows select only their exact "pack_id" field.
fn retired_index<S: OriginalAuthorityOwner>(
    doc: &Document,
    item: usize,
    meter: &mut Meter<'_, '_, S>,
) -> Result<Option<usize>> {
    let id = if doc.entries(item).is_some() {
        let Some(id) = lookup(doc, item, "pack_id", meter)? else {
            return Ok(None);
        };
        id
    } else {
        item
    };
    meter.charge_work(RETIREMENTS.len() as u64 * 32)?;
    Ok(RETIREMENTS
        .iter()
        .position(|retirement| doc.string_is(id, retirement.pack)))
}

fn migrate_document<S: OriginalAuthorityOwner>(
    doc: &Document,
    original: usize,
    profile: &CoreValue,
    selected: [bool; RETIREMENTS.len()],
    inventory: &mut impl FnMut(&Retirement) -> Result<(Digest, usize)>,
    meter: &mut Meter<'_, '_, S>,
) -> Result<CoreValue> {
    let budget = meter.budget;
    let packs = lookup(doc, original, "packs", meter)?
        .and_then(|id| array(doc, id))
        .expect("selected packs");
    let kept = profile.get_key("packs").expect("selected packs");
    let kept = kept.array().expect("selected list");
    let mut next_packs = CoreValue::array_builder(budget, kept.len()).map_err(capacity)?;
    for (&node, item) in packs.iter().zip(kept.iter()) {
        meter.charge_work(1)?;
        if retired_index(doc, node, meter)?.is_none() {
            next_packs.push(item).map_err(capacity)?;
        }
    }

    let existing = profile.get_key("content_projections");
    let existing_items = lookup(doc, original, "content_projections", meter)?
        .and_then(|id| array(doc, id))
        .unwrap_or_default();
    let mut descriptors = [const { None }; RETIREMENTS.len()];
    for (position, retirement) in RETIREMENTS.iter().enumerate() {
        if !selected[position] {
            continue;
        }
        // The source resolves even when the existing descriptor wins.
        let (digest, count) = inventory(retirement)?;
        let mut present = false;
        for &projection in existing_items {
            meter.charge_work(1)?;
            if lookup(doc, projection, "projection_id", meter)?
                .is_some_and(|id| doc.string_is(id, retirement.id))
            {
                present = true;
                break;
            }
        }
        if !present {
            descriptors[position] = Some(object(
                [
                    ("projection_id", string(retirement.id, budget)?),
                    ("kind", string(retirement.kind, budget)?),
                    ("artifact_root", string(retirement.artifact_root, budget)?),
                    ("content_digest", string(digest.as_str(), budget)?),
                    ("file_count", integer_value(count, budget)?),
                    ("source_legacy_pack_id", string(retirement.pack, budget)?),
                ],
                budget,
            )?);
        }
    }
    let projections = order_projections(
        doc,
        existing.as_ref().and_then(CoreValue::array),
        existing_items,
        descriptors,
        meter,
    )?;
    update_object(
        profile,
        [
            ("packs", next_packs.finish().map_err(capacity)?),
            ("content_projections", projections),
        ],
        budget,
    )
}

fn order_projections<S: OriginalAuthorityOwner>(
    doc: &Document,
    existing: Option<CoreArray<'_>>,
    existing_items: &[usize],
    descriptors: [Option<CoreValue>; RETIREMENTS.len()],
    meter: &mut Meter<'_, '_, S>,
) -> Result<CoreValue> {
    let size = existing_items.len() + descriptors.len();
    let _allocation = meter.op.reserve(
        OriginalAuthorityStage::Source,
        size.saturating_mul(size_of::<(CoreBuffer, usize, CoreValue)>()),
    )?;
    let mut projections = Vec::new();
    projections.try_reserve_exact(size).map_err(capacity)?;
    if projections.capacity() != size {
        return Err(capacity(()));
    }
    let mut longest = 0;
    if let Some(existing) = existing {
        for (&node, value) in existing_items.iter().zip(existing.iter()) {
            meter.charge_work(1)?;
            if value.object().is_none() {
                continue;
            }
            let id = lookup(doc, node, "projection_id", meter)?
                .ok_or_else(|| raw("builtins.KeyError", "'projection_id'"))?;
            let key = successor_selection::python_str(doc, id, meter)?;
            longest = longest.max(key.len());
            projections.push((key, projections.len(), value));
        }
    }
    for (retirement, descriptor) in RETIREMENTS.iter().zip(descriptors) {
        if let Some(descriptor) = descriptor {
            let key =
                CoreBuffer::copy_from(retirement.id.as_bytes(), meter.budget).map_err(capacity)?;
            longest = longest.max(key.len());
            projections.push((key, projections.len(), descriptor));
        }
    }
    let count = projections.len();
    if count > 1 {
        meter.charge_work(
            (count as u64)
                .saturating_mul(u64::from(count.ilog2() + 1))
                .saturating_mul((longest as u64).saturating_add(1)),
        )?;
        // A secondary source position gives Python's stable tie order.
        projections.sort_unstable_by(|left, right| {
            left.0
                .as_bytes()
                .cmp(right.0.as_bytes())
                .then(left.1.cmp(&right.1))
        });
    }
    CoreValue::array_shared(
        projections.into_iter().map(|(_, _, value)| Ok(value)),
        meter.budget,
    )
    .map_err(capacity)
}

fn migrate_ledger<S: OriginalAuthorityOwner>(
    doc: &Document,
    migrations: Option<&CoreValue>,
    record: CoreValue,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    if let Some(migrations) = migrations.filter(|value| value.object().is_some()) {
        return update_object(migrations, [(MIGRATION_ID, record)], budget);
    }
    let legacy = field(doc, doc.root, "legacy");
    let Some((source, value)) = doc
        .get(legacy, "projection_migrations")
        .filter(|&id| truthy(doc.node(id)))
        .zip(migrations)
    else {
        return object([(MIGRATION_ID, record)], budget);
    };
    pair_ledger(doc, source, value, record, op, budget)
}

fn pair_ledger<S: OriginalAuthorityOwner>(
    doc: &Document,
    source: usize,
    value: &CoreValue,
    record: CoreValue,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    // Python dict accepts sequence pairs in old metadata as well as objects.
    // Rebuild through the original decoded nodes to preserve pair order.
    let Some(items) = array(doc, source) else {
        if text(doc, source).is_some() {
            return Err(raw(
                "builtins.ValueError",
                "dictionary update sequence element #0 has length 1; 2 is required",
            ));
        }
        let kind = match doc.node(source) {
            Node::Scalar(CanonValue::Bool(_)) => "bool",
            _ => "int",
        };
        return Err(raw(
            "builtins.TypeError",
            format!("'{kind}' object is not iterable"),
        ));
    };
    let values = value.array().expect("validated sequence");
    let _funding = op.reserve(
        OriginalAuthorityStage::Source,
        (items.len() + 1).saturating_mul(size_of::<(CoreValue, CoreValue)>()),
    )?;
    let mut fields: Vec<(CoreValue, CoreValue)> = Vec::new();
    fields
        .try_reserve_exact(items.len() + 1)
        .map_err(capacity)?;
    if fields.capacity() != items.len() + 1 {
        return Err(capacity(()));
    }
    let mut nontext = false;
    for (index, (&item, value)) in items.iter().zip(values.iter()).enumerate() {
        charge(op, budget, 1)?;
        let (key, value) = pair(doc, item, value, index, budget)?;
        if key.as_str().is_none() {
            match key.inspect() {
                CoreRef::Arr(_) => {
                    return Err(raw("builtins.TypeError", "unhashable type: 'list'"));
                }
                CoreRef::Obj(_) => {
                    return Err(raw("builtins.TypeError", "unhashable type: 'dict'"));
                }
                _ => nontext = true,
            }
            continue;
        }
        let mut existing = None;
        for (seen, item) in &mut fields {
            charge(
                op,
                budget,
                1 + key.as_str().unwrap_or_default().len() as u64,
            )?;
            if seen.as_str() == key.as_str() {
                existing = Some(item);
                break;
            }
        }
        if let Some(existing) = existing {
            *existing = value;
        } else {
            fields.push((key, value));
        }
    }
    if nontext {
        return Err(json::strict_error("JSON object keys must be strings"));
    }
    let mut replaced = false;
    for (key, value) in &mut fields {
        if key.as_str() == Some(MIGRATION_ID) {
            *value = record.share();
            replaced = true;
            break;
        }
    }
    if !replaced {
        fields.push((string(MIGRATION_ID, budget)?, record));
    }
    CoreValue::object_shared(
        fields
            .iter()
            .map(|(key, value)| Ok((key.as_str().expect("string key"), value.share()))),
        budget,
    )
    .map_err(capacity)
}

fn pair(
    doc: &Document,
    item: usize,
    value: CoreValue,
    index: usize,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(CoreValue, CoreValue)> {
    let length = match doc.node(item) {
        Node::Array(items) => items.len(),
        Node::Object(items) => items.len(),
        Node::String(text) => text.0.len(),
        Node::Scalar(_) => {
            return Err(raw(
                "builtins.TypeError",
                format!("cannot convert dictionary update sequence element #{index} to a sequence"),
            ));
        }
    };
    if length != 2 {
        return Err(raw(
            "builtins.ValueError",
            format!(
                "dictionary update sequence element #{index} has length {length}; 2 is required"
            ),
        ));
    }
    let char_value = |point| {
        let mut bytes = [0; 4];
        let point = char::from_u32(point).expect("strict registry UTF-8");
        string(point.encode_utf8(&mut bytes), budget)
    };
    match doc.node(item) {
        Node::Array(_) => Ok((
            value.get_index(0).expect("pair key"),
            value.get_index(1).expect("pair value"),
        )),
        Node::Object(items) => {
            let key = |index: usize| -> Result<CoreValue> {
                let points = &items[index].0.0;
                let mut rendered = CoreBuffer::builder(budget, points.len().saturating_mul(4));
                for &point in points {
                    let mut bytes = [0; 4];
                    let point = char::from_u32(point).expect("strict registry UTF-8");
                    rendered
                        .append(point.encode_utf8(&mut bytes).as_bytes())
                        .map_err(capacity)?;
                }
                let rendered = rendered.finish().map_err(capacity)?;
                CoreValue::string_from_utf8(&rendered, budget).map_err(capacity)
            };
            Ok((key(0)?, key(1)?))
        }
        Node::String(text) => Ok((char_value(text.0[0])?, char_value(text.0[1])?)),
        Node::Scalar(_) => unreachable!("validated pair"),
    }
}

fn object<const N: usize>(
    fields: [(&str, CoreValue); N],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    CoreValue::object_shared(fields.into_iter().map(Ok), budget).map_err(capacity)
}

fn update_object<const N: usize>(
    source: &CoreValue,
    replacements: [(&'static str, CoreValue); N],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let source = source.object().expect("existing object");
    let mut new = replacements
        .iter()
        .filter(|(key, _)| source.get(key).is_none());
    let length = source.len() + new.clone().count();
    let mut original = source.iter();
    CoreValue::object_shared(
        (0..length).map(|_| -> std::result::Result<_, AllocationError> {
            if let Some((key, value)) = original.next() {
                let value = replacements
                    .iter()
                    .find(|(name, _)| *name == key)
                    .map_or(value, |(_, replacement)| replacement.share());
                Ok((key, value))
            } else {
                let (key, value) = new.next().expect("new object field");
                Ok((*key, value.share()))
            }
        }),
        budget,
    )
    .map_err(capacity)
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn string(value: &str, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue> {
    CoreValue::string(value, budget).map_err(capacity)
}

fn integer_value(value: usize, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue> {
    let value = i64::try_from(value).map_err(capacity)?;
    CoreValue::int(value, budget).map_err(capacity)
}
