//! Replay the packaged proposals for one verified interrupted source update.
use super::*;
use tobkiri_canon::{CoreArray, CoreRef};

const EDGE_KEYS: &[&str] = &[
    "caller_function_id",
    "target_provider_id",
    "contract_id",
    "operation_id",
];
const SEMANTICS: &str = "semantics_digest";

pub(super) fn matches<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    predecessor: &CoreValue,
    source: &CoreValue,
    current_digest: &Digest,
    successor_required: bool,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    for additions in [false, true] {
        op.check(STAGE)?;
        match proposal(
            op,
            predecessor,
            source,
            successor_required,
            additions,
            budget,
        ) {
            Err(error) if error.class == CONFLICT => continue,
            Err(error) => return Err(error),
            Ok(proposed) => {
                if &proposed.digest_guest(budget).map_err(capacity)? == current_digest {
                    return Ok(true);
                }
            }
        }
    }
    Ok(false)
}

fn proposal<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    predecessor: &CoreValue,
    source: &CoreValue,
    successor_required: bool,
    additions: bool,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let mut candidate = predecessor.share();
    if successor_required {
        candidate = set(&candidate, "shell", lookup(source, "shell")?, budget)?;
    }
    if successor_required || additions {
        candidate = scope_successor(op, &candidate, source, budget)?;
    }
    if additions {
        candidate = source_additions(op, &candidate, source, budget)?;
    }
    Ok(candidate)
}

fn scope_successor<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    current: &CoreValue,
    source: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let source_edges = lookup(source, "requested_edges")?;
    let source_edges = rows(&source_edges)?;
    for (index, edge) in source_edges.iter().enumerate() {
        let key = identity(&edge, EDGE_KEYS)?;
        if find(op, &source_edges, index, &key, EDGE_KEYS, budget)?.is_some() {
            return Err(raw(CONFLICT, "scope successor contains duplicate edges"));
        }
    }
    let current_edges = lookup(current, "requested_edges")?;
    let current_edges = rows(&current_edges)?;
    let mut next = CoreValue::array_builder(budget, current_edges.len()).map_err(capacity)?;
    for edge in current_edges.iter() {
        let key = identity(&edge, EDGE_KEYS)?;
        let replacement = find(
            op,
            &source_edges,
            source_edges.len(),
            &key,
            EDGE_KEYS,
            budget,
        )?;
        let edge = match replacement {
            Some(replacement) => renew_scope(edge, &replacement, budget)?,
            None => edge,
        };
        next.push(edge).map_err(capacity)?;
    }
    set(
        current,
        "requested_edges",
        next.finish().map_err(capacity)?,
        budget,
    )
}

fn renew_scope(
    edge: CoreValue,
    replacement: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    const SCOPE: &str = "requested_scope_template";
    let proposed_scope = lookup(replacement, SCOPE)?;
    let old_scope = lookup(&edge, SCOPE)?;
    if proposed_scope.object().is_none() || old_scope.object().is_none() {
        return Err(OriginalAuthorityError::unavailable());
    }
    let proposed_digest = proposed_scope.get_key(SEMANTICS);
    if let Some(digest) =
        proposed_digest.filter(|digest| !matches!(digest.inspect(), CoreRef::Null))
        && equal_except(&edge, replacement, SCOPE, budget)?
        && equal_except(&old_scope, &proposed_scope, SEMANTICS, budget)?
    {
        let unchanged = match old_scope.get_key(SEMANTICS) {
            Some(old) => definitions::python_equal(&old, &digest, budget)?,
            None => false,
        };
        if !unchanged {
            let scope = set(&old_scope, SEMANTICS, digest, budget)?;
            return set(&edge, SCOPE, scope, budget);
        }
    }
    Ok(edge)
}

fn source_additions<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    current: &CoreValue,
    source: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    for field in ["profile_id", "base", "shell"] {
        if !definitions::python_equal(&lookup(current, field)?, &lookup(source, field)?, budget)? {
            return Err(raw(
                CONFLICT,
                format!("additive Profile update cannot replace {field}"),
            ));
        }
    }
    let mut result = current.share();
    for (field, keys) in [("packs", &["pack_id"][..]), ("requested_edges", EDGE_KEYS)] {
        let old = lookup(current, field)?;
        let new = lookup(source, field)?;
        let merged = merge_rows(op, rows(&old)?, rows(&new)?, field, keys, budget)?;
        result = set(&result, field, merged, budget)?;
    }
    Ok(result)
}

fn merge_rows<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    current: CoreArray<'_>,
    source: CoreArray<'_>,
    field: &str,
    keys: &[&str],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let maximum = current
        .len()
        .checked_add(source.len())
        .ok_or_else(|| capacity(()))?;
    let mut merged = CoreValue::array_builder(budget, maximum).map_err(capacity)?;
    for (items, append) in [(&current, false), (&source, true)] {
        for (index, row) in items.iter().enumerate() {
            let key = identity(&row, keys)?;
            if find(op, items, index, &key, keys, budget)?.is_some() {
                return Err(raw(
                    CONFLICT,
                    format!("additive Profile update contains duplicate {field}"),
                ));
            }
            if append && let Some(prior) = find(op, &current, current.len(), &key, keys, budget)? {
                let matches = definitions::python_equal(&prior, &row, budget)?
                    || (field == "packs" && retains_pack_pin(&prior, &row, budget)?);
                if !matches {
                    return Err(raw(
                        CONFLICT,
                        format!("additive Profile update conflicts with existing {field}"),
                    ));
                }
                continue;
            }
            merged.push(row).map_err(capacity)?;
        }
    }
    merged.finish().map_err(capacity)
}

fn retains_pack_pin(
    current: &CoreValue,
    source: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    const PIN: &str = "artifact_digest";
    let Some(pin) = current.get_key(PIN) else {
        return Ok(false);
    };
    if !pin.as_str().is_some_and(schema::valid_digest)
        || !source
            .get_key(PIN)
            .is_some_and(|digest| matches!(digest.inspect(), CoreRef::Null))
    {
        return Ok(false);
    }
    equal_except(current, source, PIN, budget)
}

type Identity = [Option<CoreValue>; 4];

fn identity(row: &CoreValue, keys: &[&str]) -> Result<Identity> {
    let mut result = [None, None, None, None];
    for (output, key) in result.iter_mut().zip(keys) {
        let value = lookup(row, key)?;
        if value.as_str().is_none() {
            return Err(OriginalAuthorityError::unsupported(
                STAGE,
                "non-string Profile selection identity",
            ));
        }
        *output = Some(value);
    }
    Ok(result)
}

fn find<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    items: &CoreArray<'_>,
    end: usize,
    expected: &Identity,
    keys: &[&str],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreValue>> {
    for row in items.iter().take(end) {
        op.check(STAGE)?;
        let actual = identity(&row, keys)?;
        let mut matches = true;
        for (left, right) in expected.iter().flatten().zip(actual.iter().flatten()) {
            let left = left.as_str().expect("checked identity");
            let right = right.as_str().expect("checked identity");
            charge(budget, left.len().min(right.len()) + 1)?;
            if left != right {
                matches = false;
                break;
            }
        }
        if matches {
            return Ok(Some(row));
        }
    }
    Ok(None)
}

fn lookup(value: &CoreValue, key: &str) -> Result<CoreValue> {
    value
        .get_key(key)
        .ok_or_else(|| raw("builtins.KeyError", format!("'{key}'")))
}

fn rows(value: &CoreValue) -> Result<CoreArray<'_>> {
    value
        .array()
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn equal_except(
    left: &CoreValue,
    right: &CoreValue,
    omitted: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    let left = left
        .object()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let right = right
        .object()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let count = |object: &tobkiri_canon::CoreObject<'_>| {
        object.len() - usize::from(object.get(omitted).is_some())
    };
    if count(&left) != count(&right) {
        return Ok(false);
    }
    let left = left.iter().filter(|(key, _)| *key != omitted);
    let right = right.iter().filter(|(key, _)| *key != omitted);
    for ((left_key, left), (right_key, right)) in left.zip(right) {
        charge(budget, left_key.len() + 1)?;
        if left_key != right_key || !definitions::python_equal(&left, &right, budget)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn set(
    value: &CoreValue,
    key: &str,
    replacement: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let object = value
        .object()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    if object.get(key).is_some() {
        CoreValue::object_shared(
            object.iter().map(|(name, child)| {
                let value = if name == key {
                    replacement.share()
                } else {
                    child
                };
                Ok((name, value))
            }),
            budget,
        )
        .map_err(capacity)
    } else {
        // A bounded one-entry patch is needed only when the predecessor has
        // no semantics pin. Use the same owning object builder as replacements.
        let mut append = object
            .iter()
            .map(Ok)
            .chain(std::iter::once(Ok((key, replacement))));
        CoreValue::object_shared(
            (0..object.len() + 1).map(|_| append.next().expect("known object size")),
            budget,
        )
        .map_err(capacity)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    fn parse(value: &Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
        CoreValue::parse_guest(&serde_json::to_vec(value).unwrap(), budget).unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn replay_accepts_only_the_packaged_shell_scope_and_additive_proposals() {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let pin = "sha256:1111111111111111111111111111111111111111111111111111111111111111";
            let edge = |operation_id, scope| {
                json!({
                    "caller_function_id":"local.app:call",
                    "target_provider_id":"local.host",
                    "contract_id":"local.contract",
                    "operation_id":operation_id,
                    "requested_scope_template":scope,
                    "authority_mode":"explicit"
                })
            };
            let predecessor = json!({
                "profile_id":"defaults",
                "display_name":"User display name",
                "base":{"pack_id":"base"},
                "shell":{"pack_id":"shell","artifact_digest":"old"},
                "packs":[{"pack_id":"kept","artifact_digest":pin}],
                "requested_edges":[
                    edge("other", json!({"semantics_digest":"old","paths":["user"]})),
                    edge("sync", json!({"paths":["original"]}))
                ]
            });
            let mut source = predecessor.clone();
            source["display_name"] = json!("Packaged display name");
            source["shell"]["artifact_digest"] = json!("new");
            source["requested_edges"][0]["requested_scope_template"] =
                json!({"semantics_digest":"new","paths":["packaged"]});
            source["requested_edges"][1]["requested_scope_template"]["semantics_digest"] =
                json!("new");
            source["packs"] = json!([
                {"pack_id":"kept","artifact_digest":null},
                {"pack_id":"added","artifact_digest":null}
            ]);

            let mut shell_only = predecessor.clone();
            shell_only["shell"] = source["shell"].clone();
            shell_only["requested_edges"][1] = source["requested_edges"][1].clone();
            let verify = |current: &Value, source: &Value, successor_required| {
                matches(
                    &op,
                    &parse(&predecessor, budget),
                    &parse(source, budget),
                    &parse(current, budget).digest_guest(budget).unwrap(),
                    successor_required,
                    budget,
                )
                .unwrap()
            };
            assert!(verify(&shell_only, &source, true));
            assert!(!verify(&shell_only, &source, false));

            // The source additions proposal cannot replace a user scope.
            let mut added = shell_only.clone();
            added["packs"]
                .as_array_mut()
                .unwrap()
                .push(source["packs"][1].clone());
            assert!(!verify(&added, &source, true));
            source["requested_edges"][0] = predecessor["requested_edges"][0].clone();
            assert!(verify(&added, &source, true));

            let mut unrelated = added.clone();
            unrelated["display_name"] = source["display_name"].clone();
            assert!(!verify(&unrelated, &source, true));
            let duplicate = source["requested_edges"][1].clone();
            source["requested_edges"]
                .as_array_mut()
                .unwrap()
                .push(duplicate);
            assert!(!verify(&added, &source, true));
        });
    }
}
