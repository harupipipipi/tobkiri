//! Compare the re-resolved candidate with the frozen activation graph.
use super::*;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::mem::size_of;

const ARTIFACT_CHANGED: &str = "legacy activation artifact closure changed during migration";
const PRINCIPAL_CHANGED: &str = "legacy activation principal binding changed during migration";
const EDGE_CHANGED: &str = "legacy activation edge set changed during migration";
const SCOPE_CHANGED: &str = "legacy activation requested scope changed during migration";
const SCOPE_KEYS: [&str; 6] = [
    "capability",
    "semantics_digest",
    "dimensions",
    "quotas",
    "exact_request_digest",
    "opaque",
];

/// Accept only a resolved successor that preserves the predecessor's sealed inputs.
pub(in crate::original_authority::io::publication) fn verify<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    predecessor: SnapshotRecords<'_>,
    candidate: CoreValue,
) -> Result<LegacySuccessor> {
    op.check(GRAPH)?;
    let resolved = candidate.clone();
    let encoded = candidate.encode_guest(budget).map_err(|_| capacity())?;
    drop(candidate);
    let document = json::decode_read_only_strict(encoded.as_bytes(), op, budget)?;
    drop(encoded);
    let candidate = View {
        doc: &document,
        id: document.root,
    };
    verify_closure(op, budget, predecessor, candidate)?;
    verify_principals(
        op,
        budget,
        predecessor.plan.field("bindings")?,
        candidate.field("plan")?.field("bindings")?,
    )?;
    let profile = candidate.field("profile")?;
    verify_edges(
        op,
        budget,
        predecessor.profile.field("requested_edges")?,
        profile.field("requested_edges")?,
    )?;
    let digest = candidate.field("plan")?.field("plan_digest")?;
    let rendered = definitions::render_python_str(op, budget, digest.doc, digest.id)?;
    let plan_digest = rendered
        .as_str()
        .ok()
        .and_then(|value| Digest::parse(value).ok())
        .ok_or_else(|| {
            OriginalAuthorityError::unsupported(GRAPH, "legacy successor Plan digest")
        })?;
    let timestamp = predecessor
        .activation
        .get("committed_at")
        .filter(|value| matches!(value.node(), Node::String(text) if !text.0.is_empty()))
        .unwrap_or(predecessor.activation.field("created_at")?);
    let timestamp = definitions::render_python_str(op, budget, timestamp.doc, timestamp.id)?;
    let created_at = CoreValue::string(timestamp.as_str().map_err(|_| capacity())?, budget)
        .map_err(|_| capacity())?;
    Ok(LegacySuccessor {
        artifact_profile: retain_artifact_profile(op, budget, profile)?,
        plan_digest,
        resolved,
        created_at,
    })
}

fn capacity() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(GRAPH)
}

fn require(condition: bool, message: &'static str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(raw(APP_DENIED, message))
    }
}

fn verify_closure<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    predecessor: SnapshotRecords<'_>,
    successor: View<'_>,
) -> Result<()> {
    let plan = successor.field("plan")?;
    let lock = successor.field("lock")?;
    let effective = predecessor.lock.field("effective_set")?;
    const PACK_PIN: &[&str] = &["pack_id", "artifact_digest"];
    const PLAN_SHELL: &[&str] = &["provider_id", "pack_id", "artifact_digest", "contract_id"];
    // Frozen v4 lock Shell has these seven required fields. The v5 executable
    // pin is allowed in the successor; the comparison projects it away.
    const LOCK_SHELL: &[&str] = &[
        "provider_id",
        "pack_id",
        "artifact_digest",
        "definition_revision",
        "contract_id",
        "platform",
        "architecture",
    ];
    require(
        effective.same(plan.field("effective_set")?, op, budget)?
            && {
                let digest = current::closure_digest(
                    plan.field("content_projections")?,
                    effective,
                    op,
                    budget,
                )?;
                plan.field("closure_digest")?.is(digest.as_str())
            }
            && predecessor
                .lock
                .field("base")?
                .same(lock.field("base")?, op, budget)?
            && same_fields(
                op,
                budget,
                predecessor.plan.field("base")?,
                plan.field("base")?,
                PACK_PIN,
            )?
            && same_fields(
                op,
                budget,
                predecessor.lock.field("shell")?,
                lock.field("shell")?,
                LOCK_SHELL,
            )?
            && same_fields(
                op,
                budget,
                predecessor.plan.field("shell")?,
                plan.field("shell")?,
                PLAN_SHELL,
            )?,
        ARTIFACT_CHANGED,
    )
}

fn same_fields<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    left: View<'_>,
    right: View<'_>,
    fields: &[&'static str],
) -> Result<bool> {
    for &field in fields {
        if !left.field(field)?.same(right.field(field)?, op, budget)? {
            return Ok(false);
        }
    }
    Ok(true)
}

type Key<'a, const N: usize> = [&'a Text; N];

fn principal_key(binding: View<'_>) -> Result<Key<'_, 3>> {
    Ok([
        binding
            .field("function_principal")?
            .field("function_id")?
            .text()?,
        binding.field("contract_id")?.text()?,
        binding.field("operation_id")?.text()?,
    ])
}

fn edge_key(edge: View<'_>) -> Result<Key<'_, 4>> {
    // The validated IDs exclude the delimiter in Python's joined key.
    Ok([
        edge.field("caller_function_id")?.text()?,
        edge.field("target_provider_id")?.text()?,
        edge.field("contract_id")?.text()?,
        edge.field("operation_id")?.text()?,
    ])
}

struct KeyedRow<'a, const N: usize> {
    hash: u64,
    order: usize,
    key: Key<'a, N>,
    value: View<'a>,
}

impl<'a, const N: usize> KeyedRow<'a, N> {
    fn read<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        value: View<'a>,
        order: usize,
        key_of: impl FnOnce(View<'a>) -> Result<Key<'a, N>>,
    ) -> Result<Self> {
        let key = key_of(value)?;
        let mut hash = DefaultHasher::new();
        for field in key {
            compare_work(op, budget, 1 + field.0.len())?;
            field.0.hash(&mut hash);
        }
        Ok(Self {
            hash: hash.finish(),
            order,
            key,
            value,
        })
    }
}

fn scratch<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    count: usize,
) -> Result<Funded<Vec<T>>> {
    let bytes = count.checked_mul(size_of::<T>()).ok_or_else(capacity)?;
    let reservation = op.reserve(GRAPH, bytes)?;
    let mut value = Vec::new();
    value.try_reserve_exact(count).map_err(|_| capacity())?;
    if value.capacity() > count {
        return Err(capacity());
    }
    Ok(Funded {
        value,
        _capacity: reservation,
    })
}

fn search_cost(count: usize) -> usize {
    (usize::BITS - count.leading_zeros()) as usize + 1
}

fn sort_work<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    count: usize,
    width: usize,
) -> Result<()> {
    let work = count
        .checked_mul(search_cost(count))
        .and_then(|work| work.checked_mul(width))
        .ok_or_else(capacity)?;
    compare_work(op, budget, work)
}

fn index<'a, const N: usize, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    rows: View<'a>,
    key_of: impl Fn(View<'a>) -> Result<Key<'a, N>>,
) -> Result<Funded<Vec<KeyedRow<'a, N>>>> {
    let ids = rows.items()?;
    let mut entries = scratch(op, ids.len())?;
    for (order, &id) in ids.iter().enumerate() {
        entries.push(KeyedRow::read(op, budget, rows.at(id), order, &key_of)?);
    }
    sort_work(op, budget, entries.len(), size_of::<KeyedRow<'_, N>>())?;
    entries.sort_unstable_by_key(|entry| (entry.hash, entry.order));
    Ok(entries)
}

fn find<'a, const N: usize, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    rows: &'a [KeyedRow<'a, N>],
    needle: &KeyedRow<'_, N>,
) -> Result<Option<&'a KeyedRow<'a, N>>> {
    compare_work(op, budget, search_cost(rows.len()))?;
    let end = rows.partition_point(|row| row.hash <= needle.hash);
    // A comprehension preserves the last matching edge. Only the hash bucket
    // is searched; equality still tests the full key and charges each test.
    'bucket: for row in rows[..end]
        .iter()
        .rev()
        .take_while(|row| row.hash == needle.hash)
    {
        for (&left, &right) in row.key.iter().zip(&needle.key) {
            if !equal_text(left, right, op, budget)? {
                continue 'bucket;
            }
        }
        return Ok(Some(row));
    }
    Ok(None)
}

fn verify_principals<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    predecessor: View<'_>,
    successor: View<'_>,
) -> Result<()> {
    const FIELDS: &[&str] = &[
        "pack_id",
        "artifact_digest",
        "function_principal",
        "contract_id",
        "operation_id",
        "domain_kind",
    ];
    let predecessor = index(op, budget, predecessor, principal_key)?;
    let successor = index(op, budget, successor, principal_key)?;
    require(predecessor.len() == successor.len(), PRINCIPAL_CHANGED)?;
    for entries in [&predecessor[..], &successor[..]] {
        for row in entries {
            let latest = find(op, budget, entries, row)?;
            require(
                latest.is_some_and(|latest| latest.order == row.order),
                PRINCIPAL_CHANGED,
            )?;
        }
    }
    for row in &predecessor[..] {
        let counterpart =
            find(op, budget, &successor, row)?.ok_or_else(|| raw(APP_DENIED, PRINCIPAL_CHANGED))?;
        require(
            same_fields(op, budget, row.value, counterpart.value, FIELDS)?,
            PRINCIPAL_CHANGED,
        )?;
    }
    Ok(())
}

fn verify_edges<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    predecessor: View<'_>,
    successor: View<'_>,
) -> Result<()> {
    let legacy = index(op, budget, predecessor, edge_key)?;
    let successor = index(op, budget, successor, edge_key)?;
    for (left, right) in [(&legacy[..], &successor[..]), (&successor[..], &legacy[..])] {
        for row in left {
            require(find(op, budget, right, row)?.is_some(), EDGE_CHANGED)?;
        }
    }
    // Scope errors are evaluated in the original persisted Profile order.
    for (order, &id) in predecessor.items()?.iter().enumerate() {
        let edge = predecessor.at(id);
        let row = KeyedRow::read(op, budget, edge, order, edge_key)?;
        let matched = find(op, budget, &successor, &row)?.expect("equal edge sets");
        require(
            verify_scope(op, budget, edge, matched.value)?,
            SCOPE_CHANGED,
        )?;
    }
    Ok(())
}

fn scope_error(message: impl Into<String>) -> OriginalAuthorityError {
    caused(
        raw(
            APP_DENIED,
            "legacy activation requested scope cannot be reconstructed",
        ),
        raw("tobkiri_protocol.errors.ProtocolError", message),
    )
}

fn object(value: View<'_>) -> Option<&[(Text, usize)]> {
    value.doc.entries(value.id)
}

fn member<'a, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    value: View<'a>,
    key: &Text,
) -> Result<Option<View<'a>>> {
    // Match the borrowed key exactly without allocating its UTF-8 form or
    // depending on object member order in either record.
    for (name, id) in object(value).unwrap_or_default() {
        if equal_text(name, key, op, budget)? {
            return Ok(Some(value.at(*id)));
        }
    }
    Ok(None)
}

fn check_unknown<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    fields: &[(Text, usize)],
) -> Result<()> {
    let known = |key: &Text| SCOPE_KEYS.iter().any(|name| key.is(name));
    let scan = fields.len() * SCOPE_KEYS.iter().map(|name| name.len() + 1).sum::<usize>();
    compare_work(op, budget, scan)?;
    let count = fields.iter().filter(|(key, _)| !known(key)).count();
    if count == 0 {
        return Ok(());
    }
    compare_work(op, budget, scan)?;
    let mut unknown = scratch(op, count)?;
    let mut width = 0;
    let mut length = 0;
    for (key, _) in fields.iter().filter(|(key, _)| !known(key)) {
        unknown.push(key);
        width = width.max(key.0.len());
        length += 4 * key.0.len() + 2;
    }
    sort_work(op, budget, count, 2 * width + size_of::<&Text>())?;
    unknown.sort_unstable();
    const PREFIX: &str = "requested scope template contains unknown fields: ";
    compare_work(op, budget, PREFIX.len() + length)?;
    let _capacity = op.reserve(GRAPH, PREFIX.len() + length)?;
    let mut message = String::new();
    message
        .try_reserve_exact(PREFIX.len() + length)
        .map_err(|_| capacity())?;
    message.push_str(PREFIX);
    for (i, key) in unknown.iter().enumerate() {
        if i != 0 {
            message.push_str(", ");
        }
        message.extend(
            key.0
                .iter()
                .map(|point| char::from_u32(*point).expect("strict Unicode")),
        );
    }
    Err(scope_error(message))
}

fn verify_scope<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    legacy: View<'_>,
    successor: View<'_>,
) -> Result<bool> {
    let template = legacy.field("requested_scope_template")?;
    let actual = successor.field("requested_scope_template")?;
    let semantics = actual.field("semantics_digest")?;
    let entries = object(template)
        .ok_or_else(|| scope_error("requested scope template must be an object"))?;
    compare_work(op, budget, entries.len() * ("dimensions".len() + 1))?;
    let legacy_dimensions = template.get("dimensions");
    let flat = !entries.is_empty() && legacy_dimensions.is_none();
    let (dimensions, quotas) = if flat {
        (entries, None)
    } else {
        check_unknown(op, budget, entries)?;
        if template
            .get("capability")
            .is_some_and(|capability| !capability.is("operation.invoke"))
        {
            return Err(scope_error(
                "requested scope capability must be operation.invoke",
            ));
        }
        if let Some(supplied) = template.get("semantics_digest")
            && !supplied.same(semantics, op, budget)?
        {
            return Err(scope_error(
                "requested scope semantics do not match the Contract revision",
            ));
        }
        let mapping = |value| {
            object(value)
                .ok_or_else(|| scope_error("requested scope dimensions and quotas must be objects"))
        };
        (
            legacy_dimensions
                .map(mapping)
                .transpose()?
                .unwrap_or_default(),
            template
                .get("quotas")
                .map(|view| mapping(view).map(|_| view))
                .transpose()?,
        )
    };

    let actual_dimensions = actual.get("dimensions");
    let mut matches = true;
    let mut dimension_count = 2;
    let contract = legacy.field("contract_id")?;
    let operation = legacy.field("operation_id")?;
    let mut contract_match = true;
    let mut operation_match = true;
    for (key, id) in dimensions {
        compare_work(op, budget, "operation".len() + "contract".len() + 2)?;
        if key.0.is_empty() {
            return Err(scope_error(
                "requested scope dimension names must be non-empty",
            ));
        }
        let value = template.at(*id);
        let exact = if key.is("contract") {
            Some((contract, &mut contract_match))
        } else if key.is("operation") {
            Some((operation, &mut operation_match))
        } else {
            dimension_count += 1;
            None
        };
        let counterpart = match (exact.is_some(), actual_dimensions) {
            (false, Some(object)) => member(op, budget, object, key)?,
            _ => None,
        };
        let equal = if flat {
            let rendered = definitions::render_python_str(op, budget, value.doc, value.id)?;
            let rendered = rendered.as_str().expect("strict Python str is UTF-8");
            if rendered.is_empty() {
                return Err(scope_error(
                    "requested scope dimension values must be strings",
                ));
            }
            if rendered == "*" {
                return Err(scope_error(
                    "requested scope templates cannot use wildcards",
                ));
            }
            let expected = if let Some((expected, _)) = &exact {
                Some(*expected)
            } else {
                single_item(counterpart)
            };
            compare_work(op, budget, rendered.len() + 1)?;
            expected.is_some_and(|expected| expected.is(rendered))
        } else {
            let Node::Array(items) = value.node() else {
                return Err(scope_error(
                    "requested scope dimension values must be strings",
                ));
            };
            let mut wildcard = false;
            if items.is_empty() {
                return Err(scope_error(
                    "requested scope dimension values must be strings",
                ));
            }
            for &id in items {
                compare_work(op, budget, 2)?;
                let Node::String(item) = value.doc.node(id) else {
                    return Err(scope_error(
                        "requested scope dimension values must be strings",
                    ));
                };
                if item.0.is_empty() {
                    return Err(scope_error(
                        "requested scope dimension values must be strings",
                    ));
                }
                wildcard |= item.is("*");
            }
            if wildcard {
                return Err(scope_error(
                    "requested scope templates cannot use wildcards",
                ));
            }
            if let Some((expected, _)) = &exact {
                let mut all = true;
                for &id in items {
                    all &= value.at(id).same(*expected, op, budget)?;
                }
                all
            } else {
                normalized_dimension_matches(op, budget, value, items, counterpart)?
            }
        };
        if let Some((_, exact_matches)) = exact {
            *exact_matches = equal;
        } else {
            matches &= equal;
        }
    }
    if !contract_match {
        return Err(scope_error(
            "requested scope contract does not match its Profile edge",
        ));
    }
    if !operation_match {
        return Err(scope_error(
            "requested scope operation does not match its Profile edge",
        ));
    }

    let actual_quotas = actual.get("quotas");
    if let Some(quotas) = quotas {
        for (key, id) in object(quotas).expect("validated quotas mapping") {
            compare_work(op, budget, 1)?;
            if key.0.is_empty()
                || !matches!(quotas.doc.node(*id), Node::Scalar(CanonValue::Int(n)) if *n >= 0)
            {
                return Err(scope_error(
                    "requested scope quotas must be non-negative integers",
                ));
            }
        }
        matches &= match actual_quotas {
            Some(actual) => quotas.same(actual, op, budget)?,
            None => false,
        };
    } else {
        matches &= actual_quotas.and_then(object).is_some_and(<[_]>::is_empty);
    }
    let null = |value: View<'_>| matches!(value.node(), Node::Scalar(CanonValue::Null));
    let false_bool =
        |value: View<'_>| matches!(value.node(), Node::Scalar(CanonValue::Bool(false)));
    if !flat
        && (!template.get("exact_request_digest").is_none_or(null)
            || !template.get("opaque").is_none_or(false_bool))
    {
        return Err(scope_error(
            "Profile requested scopes must use declarative semantics",
        ));
    }
    matches &= object(actual).is_some_and(|fields| fields.len() == SCOPE_KEYS.len())
        && actual
            .get("capability")
            .is_some_and(|value| value.is("operation.invoke"))
        && actual.get("exact_request_digest").is_some_and(null)
        && actual.get("opaque").is_some_and(false_bool)
        && actual_dimensions
            .and_then(object)
            .is_some_and(|fields| fields.len() == dimension_count);
    if let Some(dimensions) = actual_dimensions {
        for (key, expected) in [("contract", contract), ("operation", operation)] {
            compare_work(
                op,
                budget,
                object(dimensions).unwrap_or_default().len() * (key.len() + 1),
            )?;
            let actual = single_item(dimensions.get(key));
            matches &= match actual {
                Some(actual) => expected.same(actual, op, budget)?,
                None => false,
            };
        }
    }
    Ok(matches)
}

fn single_item(values: Option<View<'_>>) -> Option<View<'_>> {
    let values = values?;
    match values.node() {
        Node::Array(ids) if ids.len() == 1 => Some(values.at(ids[0])),
        _ => None,
    }
}

fn text_order<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    left: &Text,
    right: &Text,
) -> Result<std::cmp::Ordering> {
    compare_work(op, budget, 1 + left.0.len().min(right.0.len()))?;
    Ok(left.cmp(right))
}

fn normalized_dimension_matches<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    raw: View<'_>,
    items: &[usize],
    actual: Option<View<'_>>,
) -> Result<bool> {
    let Some(actual) = actual else {
        return Ok(false);
    };
    let Node::Array(actual_ids) = actual.node() else {
        return Ok(false);
    };
    if actual_ids.is_empty() || actual_ids.len() > items.len() {
        return Ok(false);
    }
    let mut found = scratch(op, actual_ids.len())?;
    compare_work(op, budget, actual_ids.len())?;
    found.resize(actual_ids.len(), false);
    let mut previous = None;
    for &id in actual_ids {
        let Node::String(item) = actual.doc.node(id) else {
            return Ok(false);
        };
        if let Some(previous) = previous
            && !text_order(op, budget, previous, item)?.is_lt()
        {
            return Ok(false);
        }
        previous = Some(item);
    }
    for &id in items {
        let item = raw.at(id).text()?;
        let (mut left, mut right) = (0, actual_ids.len());
        loop {
            if left == right {
                return Ok(false);
            }
            let mid = left + (right - left) / 2;
            match text_order(op, budget, actual.at(actual_ids[mid]).text()?, item)? {
                std::cmp::Ordering::Less => left = mid + 1,
                std::cmp::Ordering::Greater => right = mid,
                std::cmp::Ordering::Equal => {
                    found[mid] = true;
                    break;
                }
            }
        }
    }
    Ok(found.iter().all(|seen| *seen))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::io::publication::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    fn edge(template: Value) -> Value {
        json!({
            "caller_function_id": "caller",
            "target_provider_id": "provider",
            "contract_id": "example.echo.v1",
            "operation_id": "invoke",
            "requested_scope_template": template,
        })
    }

    fn scope(dimensions: Value, quotas: Value) -> Value {
        json!({
            "capability": "operation.invoke",
            "semantics_digest": Digest::hash(b"example semantics").as_str(),
            "dimensions": dimensions,
            "quotas": quotas,
            "exact_request_digest": null,
            "opaque": false,
        })
    }

    fn denied<T: std::fmt::Debug>(result: Result<T>, message: &str) -> OriginalAuthorityError {
        let error = result.unwrap_err();
        assert_eq!((error.class, error.message.as_str()), (APP_DENIED, message));
        error
    }

    #[tokio::test(flavor = "current_thread")]
    async fn legacy_successor_checks_ordered_closure_and_allows_new_pins() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let base = json!({"pack_id":"base","artifact_digest":"base-pin","definition_revision":"base-rev"});
            let shell = json!({"pack_id":"shell","artifact_digest":"shell-pin","definition_revision":"shell-rev","provider_id":"shell.provider","contract_id":"app.shell.v1","platform":"macos","architecture":"arm64"});
            let effective = json!([{"role":"base"},{"role":"shell"}]);
            let predecessor = json!({
                "profile":{"requested_edges":[]},
                "lock":{"base":base,"shell":shell,"effective_set":effective},
                "plan":{"base":base,"shell":shell,"bindings":[]},
                "activation":{"created_at":"2026-08-10T00:00:00Z"},
            });
            let data = serde_json::to_vec(&predecessor).unwrap();
            let document = json::decode_read_only_strict(&data, &op, budget).unwrap();
            let view = View {
                doc: &document,
                id: document.root,
            };
            let records = SnapshotRecords {
                profile: view.field("profile").unwrap(),
                lock: view.field("lock").unwrap(),
                plan: view.field("plan").unwrap(),
                activation: view.field("activation").unwrap(),
            };
            let mut successor = predecessor;
            successor["lock"]["shell"]["executable_artifact_digest"] = json!("new-pin");
            successor["profile"]["shell"] = successor["lock"]["shell"].clone();
            let plan_digest = Digest::hash(b"successor");
            successor["plan"]["plan_digest"] = json!(plan_digest.as_str());
            successor["plan"]["effective_set"] = effective;
            successor["plan"]["content_projections"] = json!([{ "projection_id": "new" }]);
            successor["plan"]["closure_digest"] = json!(Digest::hash(
                br#"{"content_projections":[{"projection_id":"new"}],"effective_set":[{"role":"base"},{"role":"shell"}]}"#
            ).as_str());
            let run = |candidate: &Value| {
                let candidate =
                    CoreValue::parse_guest(&serde_json::to_vec(candidate).unwrap(), budget)
                        .unwrap();
                verify(&op, budget, records, candidate)
            };
            let retained = run(&successor).unwrap();
            assert_eq!(retained.plan_digest, plan_digest);
            assert_eq!(
                retained
                    .artifact_profile
                    .get_key("shell")
                    .unwrap()
                    .get_key("executable_artifact_digest")
                    .unwrap()
                    .as_str(),
                Some("new-pin"),
            );
            let mut reversed = successor.clone();
            reversed["plan"]["effective_set"]
                .as_array_mut()
                .unwrap()
                .reverse();
            denied(run(&reversed), ARTIFACT_CHANGED);
            let mut changed_projection = successor.clone();
            changed_projection["plan"]["content_projections"] = json!([]);
            denied(run(&changed_projection), ARTIFACT_CHANGED);
            successor["lock"]["shell"]["platform"] = json!("linux");
            denied(run(&successor), ARTIFACT_CHANGED);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn legacy_successor_compares_principals_and_edges_by_original_keys() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let binding = json!({"pack_id":"pack","artifact_digest":"pack-pin","function_principal":{"function_id":"provider","revision":"same"},"contract_id":"example.echo.v1","operation_id":"invoke","domain_kind":"wasm_component"});
            let mut extra = binding.clone();
            extra["operation_id"] = json!("other");
            let principal_check = |successor: Value| {
                let input = json!({"old":[binding.clone(), extra.clone()],"new":successor});
                let bytes = serde_json::to_vec(&input).unwrap();
                let doc = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
                let view = View {
                    doc: &doc,
                    id: doc.root,
                };
                verify_principals(
                    &op,
                    budget,
                    view.field("old").unwrap(),
                    view.field("new").unwrap(),
                )
            };
            let mut extended = binding.clone();
            extended["authority_reference"] = json!("authority-ref:12345678");
            principal_check(json!([extra.clone(), extended.clone()])).unwrap();
            denied(
                principal_check(json!([extended.clone(), extended.clone()])),
                PRINCIPAL_CHANGED,
            );
            extended["function_principal"]["revision"] = json!("changed");
            denied(principal_check(json!([extra, extended])), PRINCIPAL_CHANGED);

            let old = edge(json!({}));
            let normalized = edge(scope(
                json!({"contract":["example.echo.v1"],"operation":["invoke"]}),
                json!({}),
            ));
            let mut stale = normalized.clone();
            stale["requested_scope_template"]["dimensions"]["region"] = json!(["new"]);
            let edges_check = |successor: Value| {
                let input = json!({"old":[old.clone()],"new":successor});
                let bytes = serde_json::to_vec(&input).unwrap();
                let doc = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
                let view = View {
                    doc: &doc,
                    id: doc.root,
                };
                verify_edges(
                    &op,
                    budget,
                    view.field("old").unwrap(),
                    view.field("new").unwrap(),
                )
            };
            edges_check(json!([stale.clone(), normalized.clone()])).unwrap();
            denied(
                edges_check(json!([normalized.clone(), stale])),
                SCOPE_CHANGED,
            );
            let mut extra = normalized.clone();
            extra["caller_function_id"] = json!("new-caller");
            denied(edges_check(json!([normalized, extra])), EDGE_CHANGED);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn legacy_successor_reconstructs_flat_and_structured_requested_scopes() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let run = |old, new| {
                let input = json!({"old":[edge(old)],"new":[edge(new)]});
                let bytes = serde_json::to_vec(&input).unwrap();
                let doc = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
                let view = View {
                    doc: &doc,
                    id: doc.root,
                };
                verify_edges(
                    &op,
                    budget,
                    view.field("old").unwrap(),
                    view.field("new").unwrap(),
                )
            };
            let structured = scope(
                json!({"contract":["example.echo.v1"], "operation":["invoke"], "region":["é", "\u{e000}", "😀"]}),
                json!({"queries":2}),
            );
            run(json!({"dimensions":{"contract":["example.echo.v1","example.echo.v1"],"region":["😀","é","\u{e000}","é"]},"quotas":{"queries":2}}), structured.clone()).unwrap();
            let flat = scope(
                json!({"contract":["example.echo.v1"], "operation":["invoke"], "region":["['é', True, None]"]}),
                json!({}),
            );
            run(json!({"region":["é",true,null]}), flat).unwrap();
            let cannot_reconstruct = "legacy activation requested scope cannot be reconstructed";
            let invalid = denied(
                run(
                    json!({"dimensions":{"region":["*",false]},"quotas":{"queries":true}}),
                    structured.clone(),
                ),
                cannot_reconstruct,
            );
            assert!(
                matches!(invalid.cause, Some(OriginalAuthorityCause::Python { class: "tobkiri_protocol.errors.ProtocolError", message, .. }) if message == "requested scope dimension values must be strings")
            );
            let invalid = denied(
                run(
                    json!({"dimensions":{"region":["😀","é","\u{e000}"]},"z":0,"a":1}),
                    structured.clone(),
                ),
                cannot_reconstruct,
            );
            assert!(
                matches!(invalid.cause, Some(OriginalAuthorityCause::Python { message, .. }) if message == "requested scope template contains unknown fields: a, z")
            );
            denied(
                run(
                    json!({"dimensions":{"region":["é"]},"quotas":{"queries":2}}),
                    structured,
                ),
                SCOPE_CHANGED,
            );
        });
    }
}
