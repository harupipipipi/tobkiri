//! Cross-record bindings of a current persisted Profile, Lock, and Plan.
use super::*;
use sha2::{Digest as _, Sha256};
use std::{collections::HashMap, hash::Hash, mem::size_of};
use tobkiri_canon::{AllocationKind, AllocationRequest};

const SOURCE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const AUDIT: OriginalAuthorityStage = OriginalAuthorityStage::Audit;
const SHARED: [&str; 10] = [
    "profile_definition_digest",
    "catalog_revision",
    "bundle_digest",
    "application",
    "effective_set",
    "content_projections",
    "requested_edges_digest",
    "constraints_digest",
    "closure_digest",
    "provenance_digest",
];

/// Check the current record graph before the independent activation checks.
pub(super) fn verify<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    records: SnapshotRecords<'_>,
    verify_projections: impl FnOnce(View<'_>, View<'_>) -> Result<()>,
) -> Result<()> {
    let SnapshotRecords {
        profile,
        lock,
        plan,
        ..
    } = records;
    let profile_revision = profile.digest(None, op, budget)?;
    let plan_digest = plan.digest(Some("plan_digest"), op, budget)?;
    let lock_digest = lock.digest(Some("lock_digest"), op, budget)?;
    require(
        lock.field("profile_revision")?
            .is(profile_revision.as_str())
            && plan
                .field("profile_revision")?
                .is(profile_revision.as_str()),
        "ProfileLock or ResolvedPlan is stale",
    )?;
    require(
        plan.field("plan_digest")?.is(plan_digest.as_str())
            && lock.field("plan_digest")?.is(plan_digest.as_str()),
        "ResolvedPlan digest is stale",
    )?;
    require(
        lock.field("lock_digest")?.is(lock_digest.as_str()),
        "ProfileLock digest is stale",
    )?;
    require(
        lock.field("security_epoch")?
            .same(plan.field("security_epoch")?, op, budget)?,
        "ProfileLock security epoch is stale",
    )?;
    for field in SHARED {
        require(
            lock.field(field)?.same(plan.field(field)?, op, budget)?,
            "ProfileLock and ResolvedPlan bindings diverge",
        )?;
    }
    let snapshot = profile.field("profile_authority_snapshot_digest")?;
    require(
        plan.field("profile_authority_snapshot_digest")?
            .same(snapshot, op, budget)?
            && lock
                .field("profile_authority_snapshot_digest")?
                .same(snapshot, op, budget)?,
        "Profile authority snapshot is stale",
    )?;
    require(
        plan.field("catalog_revision")?
            .same(profile.field("catalog_revision")?, op, budget)?,
        "Profile catalog revision is stale",
    )?;
    let edges = profile.field("requested_edges")?;
    require(
        plan.field("requested_edges_digest")?
            .is(edges.digest(None, op, budget)?.as_str()),
        "Profile requested edge set is stale",
    )?;
    require(
        plan.field("provenance_digest")?.is(profile
            .field("provenance")?
            .digest(None, op, budget)?
            .as_str()),
        "Profile provenance binding is stale",
    )?;
    let expected_closure = plan.field("closure_digest")?;
    let effective = plan.field("effective_set")?;
    let projections = plan.field("content_projections")?;
    require(
        expected_closure.is(closure_digest(projections, effective, op, budget)?.as_str()),
        "Profile closure digest is stale",
    )?;
    verify_projections(projections, effective)?;
    verify_closure(profile, plan, effective, op, budget)?;
    verify_bindings(edges, plan.field("bindings")?, op, budget)
}

pub(super) fn closure_digest<S: OriginalAuthorityOwner>(
    projections: View<'_>,
    effective: View<'_>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest> {
    // The entire Plan was canonicalized first. Its two closure subtrees retain
    // the same depth in this synthetic object, ordered by canonical key spelling.
    const PREFIX: &[u8] = b"{\"content_projections\":";
    const MIDDLE: &[u8] = b",\"effective_set\":";
    let mut hasher = Sha256::new();
    hasher.update(PREFIX);
    let encoded = projections.canonical(op, budget)?;
    charge(op, budget, AUDIT, encoded.len() + PREFIX.len())?;
    let first = encoded.len();
    hasher.update(encoded.as_slice());
    drop(encoded);
    hasher.update(MIDDLE);
    let encoded = effective.canonical(op, budget)?;
    let last = encoded.len() + MIDDLE.len() + 1;
    if first + PREFIX.len() + last > tobkiri_canon::MAX_BYTES {
        return Err(json::strict_error("canonical JSON exceeds size limit"));
    }
    charge(op, budget, AUDIT, last)?;
    hasher.update(encoded.as_slice());
    hasher.update(b"}");
    Ok(Digest::from_sha256(hasher.finalize().into()))
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct PackPin<'a> {
    name: &'a [u32],
    artifact_digest: Option<&'a [u32]>,
}

impl<'a> PackPin<'a> {
    fn read(value: View<'a>, name: &'static str) -> Result<Self> {
        let name = value.field(name)?.text()?.0.as_slice();
        let digest = value.field("artifact_digest")?;
        let artifact_digest = match digest.node() {
            Node::Scalar(CanonValue::Null) => None,
            _ => Some(digest.text()?.0.as_slice()),
        };
        Ok(Self {
            name,
            artifact_digest,
        })
    }

    fn work(self) -> usize {
        1 + self.name.len() + self.artifact_digest.map_or(0, <[u32]>::len)
    }
}

fn verify_closure<S: OriginalAuthorityOwner>(
    profile: View<'_>,
    plan: View<'_>,
    effective: View<'_>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    {
        let mut identities = index(op, budget, effective.items()?.len())?;
        for &id in effective.items()? {
            let name = effective.at(id).field("identity")?.text()?.0.as_slice();
            charge(op, budget, SOURCE, 1 + name.len())?;
            require(
                identities.insert(name, ()).is_none(),
                "Profile closure contains duplicate artifacts",
            )?;
        }
    }
    for (role, message) in [
        ("base", "Profile Base closure binding is stale"),
        ("shell", "Profile Shell closure binding is stale"),
    ] {
        require_pin(
            only_role(effective, role, op, budget)?,
            "identity",
            plan,
            role,
            message,
            op,
            budget,
        )?;
    }
    let packs = profile.field("packs")?;
    {
        let mut profile_packs = index(op, budget, packs.items()?.len())?;
        for &id in packs.items()? {
            let pin = PackPin::read(packs.at(id), "pack_id")?;
            charge(op, budget, SOURCE, pin.work())?;
            profile_packs.insert(pin, false);
        }
        let stale = || raw(APP_DENIED, "Profile Pack closure binding is stale");
        for &id in effective.items()? {
            charge(op, budget, SOURCE, 1)?;
            let row = effective.at(id);
            if row.field("role")?.is("pack") {
                let pin = PackPin::read(row, "identity")?;
                charge(op, budget, SOURCE, pin.work())?;
                *profile_packs.get_mut(&pin).ok_or_else(stale)? = true;
            }
        }
        charge(op, budget, SOURCE, profile_packs.capacity())?;
        require(
            profile_packs.values().all(|matched| *matched),
            "Profile Pack closure binding is stale",
        )?;
    }
    require_pin(
        only_role(packs, "application", op, budget)?,
        "pack_id",
        plan,
        "application",
        "Profile Application binding is stale",
        op,
        budget,
    )
}

fn only_role<'a, S: OriginalAuthorityOwner>(
    rows: View<'a>,
    role: &str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<View<'a>>> {
    let mut first = None;
    let mut unique = true;
    for &id in rows.items()? {
        charge(op, budget, SOURCE, 1)?;
        let row = rows.at(id);
        if row.get("role").is_some_and(|value| value.is(role)) && first.replace(row).is_some() {
            unique = false;
        }
    }
    Ok(first.filter(|_| unique))
}

fn require_pin<S: OriginalAuthorityOwner>(
    row: Option<View<'_>>,
    row_name: &'static str,
    plan: View<'_>,
    role: &'static str,
    message: &'static str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let row = row.ok_or_else(|| raw(APP_DENIED, message))?;
    let row = PackPin::read(row, row_name)?;
    let binding = PackPin::read(plan.field(role)?, "pack_id")?;
    charge(op, budget, SOURCE, row.work())?;
    require(row == binding, message)
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct EdgeKey<'a>([&'a [u32]; 4]);

impl<'a> EdgeKey<'a> {
    fn read(edge: View<'a>, target: View<'a>) -> Result<Self> {
        // Validated identifiers exclude '|', so the tuple is equivalent to the
        // original joined key, without allocating one string per row.
        Ok(Self([
            edge.field("caller_function_id")?.text()?.0.as_slice(),
            target.text()?.0.as_slice(),
            edge.field("contract_id")?.text()?.0.as_slice(),
            edge.field("operation_id")?.text()?.0.as_slice(),
        ]))
    }

    fn work(self) -> usize {
        1 + self.0.into_iter().map(<[u32]>::len).sum::<usize>()
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum AuthorityMode {
    ProfileGrant,
    InteractiveOnly,
}

fn authority_mode(value: View<'_>) -> Result<AuthorityMode> {
    match value.get("authority_mode") {
        None => Ok(AuthorityMode::ProfileGrant),
        Some(mode) if mode.is("profile_grant") => Ok(AuthorityMode::ProfileGrant),
        Some(mode) if mode.is("interactive_only") => Ok(AuthorityMode::InteractiveOnly),
        _ => Err(raw(APP_DENIED, "requested edge authority mode is invalid")),
    }
}

fn verify_bindings<S: OriginalAuthorityOwner>(
    edges: View<'_>,
    bindings: View<'_>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let mut edge_bindings = index(op, budget, edges.items()?.len())?;
    for &id in edges.items()? {
        let edge = edges.at(id);
        let key = EdgeKey::read(edge, edge.field("target_provider_id")?)?;
        let reference = edge.field("authority_reference")?;
        let scope = edge
            .field("requested_scope_template")?
            .digest(None, op, budget)?;
        let mode = authority_mode(edge)?;
        charge(op, budget, SOURCE, key.work())?;
        edge_bindings.insert(key, (reference, scope, mode));
    }
    let mut plan_bindings = index(op, budget, bindings.items()?.len())?;
    for &id in bindings.items()? {
        let binding = bindings.at(id);
        let target = binding.field("function_principal")?.field("function_id")?;
        let key = EdgeKey::read(binding, target)?;
        charge(op, budget, SOURCE, key.work())?;
        // The source counts all matches for a requested edge and separately
        // rejects duplicates among unrequested bindings after the edge walk.
        plan_bindings
            .entry(key)
            .and_modify(|match_row| *match_row = None)
            .or_insert(Some(binding));
    }
    const STALE: &str = "ResolvedPlan Authority binding is stale";
    for &id in edges.items()? {
        let edge = edges.at(id);
        let key = EdgeKey::read(edge, edge.field("target_provider_id")?)?;
        charge(op, budget, SOURCE, key.work() * 2)?;
        let binding = plan_bindings
            .get(&key)
            .copied()
            .flatten()
            .ok_or_else(|| raw(APP_DENIED, STALE))?;
        let actual_reference = binding.field("authority_reference")?;
        let actual_scope = binding.field("requested_scope_digest")?;
        let actual_mode = authority_mode(binding)?;
        // The source dict comprehension keeps the last row for duplicate edge keys.
        let (reference, scope, mode) = &edge_bindings[&key];
        require(
            actual_reference.same(*reference, op, budget)?
                && actual_scope.is(scope.as_str())
                && actual_mode == *mode,
            STALE,
        )?;
    }
    require(
        plan_bindings.len() == bindings.items()?.len(),
        "ResolvedPlan contains duplicate bindings",
    )
}

fn index<K: Eq + Hash, V, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    entries: usize,
) -> Result<Funded<HashMap<K, V>>> {
    charge(op, budget, SOURCE, entries)?;
    // Allow bucket rounding, load factor, and control bytes before allocating.
    let capacity = || OriginalAuthorityError::capacity(SOURCE);
    let bytes = entries
        .checked_mul(4 * (size_of::<(K, V)>() + 1))
        .and_then(|bytes| bytes.checked_add(512))
        .ok_or_else(capacity)?;
    let _capacity = op.reserve(SOURCE, bytes)?;
    let mut value = HashMap::new();
    value.try_reserve(entries).map_err(|_| capacity())?;
    Ok(Funded { value, _capacity })
}

fn require(matched: bool, message: &'static str) -> Result<()> {
    if matched {
        Ok(())
    } else {
        Err(raw(APP_DENIED, message))
    }
}

fn charge<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    stage: OriginalAuthorityStage,
    work: usize,
) -> Result<()> {
    op.check(stage)?;
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            depth: 0,
            elements: 0,
            work: work as u64,
        })
        .map_err(|_| OriginalAuthorityError::capacity(stage))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::io::publication::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    #[tokio::test(flavor = "current_thread")]
    async fn requested_binding_multiplicity_takes_precedence_over_unrequested_duplicates() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let scope = Digest::hash(b"{}");
            let edge = json!({
                "caller_function_id": "caller",
                "target_provider_id": "provider",
                "contract_id": "example.echo.v1",
                "operation_id": "invoke",
                "authority_reference": "authority-ref:12345678",
                "requested_scope_template": {},
            });
            let binding = json!({
                "caller_function_id": "caller",
                "function_principal": {"function_id": "provider"},
                "contract_id": "example.echo.v1",
                "operation_id": "invoke",
                "authority_reference": "authority-ref:12345678",
                "requested_scope_digest": scope.as_str(),
                "authority_mode": "profile_grant",
            });
            let mut extra = binding.clone();
            extra["operation_id"] = json!("extra");
            let run = |rows: Vec<Value>| {
                let input = json!({"edges": [edge.clone()], "bindings": rows});
                let bytes = serde_json::to_vec(&input).unwrap();
                let document = json::decode_read_only_strict(&bytes, &op, budget).unwrap();
                let root = View {
                    doc: &document,
                    id: document.root,
                };
                verify_bindings(
                    root.field("edges").unwrap(),
                    root.field("bindings").unwrap(),
                    &op,
                    budget,
                )
            };
            run(vec![extra.clone(), binding.clone()]).unwrap();
            let assert_denied = |result: Result<()>, expected| {
                let error = result.unwrap_err();
                assert_eq!(error.class, APP_DENIED);
                assert_eq!(error.message, expected);
            };
            assert_denied(
                run(vec![
                    extra.clone(),
                    binding.clone(),
                    extra.clone(),
                    binding.clone(),
                ]),
                "ResolvedPlan Authority binding is stale",
            );
            assert_denied(
                run(vec![extra.clone(), binding, extra]),
                "ResolvedPlan contains duplicate bindings",
            );
        });
    }
}
