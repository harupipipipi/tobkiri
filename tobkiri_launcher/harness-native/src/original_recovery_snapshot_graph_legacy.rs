//! Verify the frozen predecessor before re-resolving a legacy activation.
use super::*;
use std::hash::{DefaultHasher, Hash, Hasher};

pub(super) fn verify<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    records: SnapshotRecords<'_>,
    profile_id: &str,
    epoch_matches: impl FnOnce(View<'_>) -> Result<bool>,
    authority_matches: impl FnOnce(View<'_>) -> Result<bool>,
) -> Result<()> {
    let SnapshotRecords {
        profile,
        lock,
        plan,
        activation,
    } = records;
    let plan_digest = plan.digest(Some("plan_digest"), op, budget)?;
    let lock_digest = lock.digest(Some("lock_digest"), op, budget)?;
    let profile_revision = profile.digest(None, op, budget)?;
    if !plan
        .field("profile_revision")?
        .is(profile_revision.as_str())
        || !field_eq(lock, plan, "profile_revision", op, budget)?
        || !plan.field("plan_digest")?.is(plan_digest.as_str())
        || !field_eq(lock, plan, "plan_digest", op, budget)?
        || !lock.field("lock_digest")?.is(lock_digest.as_str())
        || !field_eq(activation, plan, "plan_digest", op, budget)?
        || !field_eq(
            activation,
            profile,
            "profile_authority_snapshot_digest",
            op,
            budget,
        )?
        || !field_eq(activation, plan, "security_epoch", op, budget)?
        || !epoch_matches(activation.field("security_epoch")?)?
        || !activation.field("state")?.is("active")
    {
        return Err(raw(
            APP_DENIED,
            "legacy activation predecessor is stale or tampered",
        ));
    }

    verify_graph(op, budget, records, profile_id)?;
    if !authority_matches(activation)? {
        return Err(raw(
            APP_DENIED,
            "legacy activation Authority record is unavailable",
        ));
    }
    verify_edges(op, budget, profile.field("requested_edges")?)
}

fn field_eq<S: OriginalAuthorityOwner>(
    left: View<'_>,
    right: View<'_>,
    field: &'static str,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    left.field(field)?.same(right.field(field)?, op, budget)
}

type ArtifactKey<'a> = (&'static str, &'a [u32], Option<&'a [u32]>);

fn artifact<'a>(
    item: View<'a>,
    role: &'static str,
    identity: &'static str,
) -> Result<(ArtifactKey<'a>, usize)> {
    let identity = item.field(identity)?.text()?.0.as_slice();
    let digest = item.field("artifact_digest")?;
    let digest = match digest.node() {
        Node::Scalar(CanonValue::Null) => None,
        _ => Some(digest.text()?.0.as_slice()),
    };
    let units = identity.len() + digest.map_or(0, <[u32]>::len) + role.len();
    Ok(((role, identity, digest), units))
}

fn verify_graph<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    records: SnapshotRecords<'_>,
    profile_id: &str,
) -> Result<()> {
    let SnapshotRecords {
        profile,
        lock,
        plan,
        activation,
    } = records;
    let inconsistent = || raw(APP_DENIED, "legacy activation record graph is inconsistent");
    let legacy_effective = lock.field("effective_set")?;
    // Python builds the profile set before inspecting any of the four IDs.
    // In particular, subscripting a null profile shell fails at this point.
    let base = artifact(profile.field("base")?, "base", "pack_id")?;
    let shell = artifact(profile.field("shell")?, "shell", "pack_id")?;
    let packs = profile.field("packs")?;
    let pack_ids = packs.items()?;
    let mut expected = Keys::new(op, pack_ids.len() + 2)?;
    expected.insert(op, budget, base)?;
    expected.insert(op, budget, shell)?;
    for id in pack_ids {
        expected.insert(op, budget, artifact(packs.at(*id), "pack", "pack_id")?)?;
    }
    for record in [profile, lock, plan, activation] {
        let id = record.field("profile_id")?.text()?;
        compare_work(op, budget, id.0.len().min(profile_id.len()) + 1)?;
        if !id.is(profile_id) {
            return Err(inconsistent());
        }
    }
    if !field_eq(lock, profile, "catalog_revision", op, budget)? {
        return Err(inconsistent());
    }
    for selection in ["base", "shell"] {
        let lock = lock.field(selection)?;
        let plan = plan.field(selection)?;
        if !field_eq(lock, plan, "pack_id", op, budget)?
            || !field_eq(lock, plan, "artifact_digest", op, budget)?
        {
            return Err(inconsistent());
        }
    }

    let effective_ids = legacy_effective.items()?;
    let mut actual = Keys::new(op, effective_ids.len())?;
    for id in effective_ids {
        let item = legacy_effective.at(*id);
        let role = item.field("role")?;
        let role = ["base", "shell", "pack"]
            .into_iter()
            .find(|candidate| role.is(candidate))
            .ok_or_else(inconsistent)?;
        actual.insert(op, budget, artifact(item, role, "identity")?)?;
    }
    expected.sort(op, budget)?;
    actual.sort(op, budget)?;
    if !expected.is_subset_of(&actual, op, budget)?
        || !actual.is_subset_of(&expected, op, budget)?
    {
        return Err(inconsistent());
    }
    Ok(())
}

fn verify_edges<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    edges: View<'_>,
) -> Result<()> {
    let ids = edges.items()?;
    if ids.is_empty() {
        return Ok(());
    }
    let mut keys = Keys::new(op, ids.len())?;
    for id in ids {
        let edge = edges.at(*id);
        // The four schema-validated strings cannot contain '|', so the tuple
        // has the same identity as Python's joined _edge_key string.
        let key = [
            edge.field("caller_function_id")?.text()?.0.as_slice(),
            edge.field("target_provider_id")?.text()?.0.as_slice(),
            edge.field("contract_id")?.text()?.0.as_slice(),
            edge.field("operation_id")?.text()?.0.as_slice(),
        ];
        keys.insert(op, budget, (key, key.iter().map(|part| part.len()).sum()))?;
    }
    keys.sort(op, budget)?;
    for (index, entry) in keys.items.iter().enumerate() {
        if Keys::contains_from(&keys.items[index + 1..], entry, op, budget)? {
            return Err(raw(APP_DENIED, "legacy activation edge set is ambiguous"));
        }
    }
    // With unique keys, the bindings comprehension preserves their set.
    for id in ids {
        compare_work(op, budget, "authority-ref:".len() + 1)?;
        let reference = edges.at(*id).get("authority_reference");
        let prefixed = reference.is_some_and(|reference| {
            matches!(
                reference.node(),
                Node::String(value)
                    if value.0.iter().copied().take("authority-ref:".len())
                        .eq("authority-ref:".bytes().map(u32::from))
            )
        });
        if !prefixed {
            return Err(raw(
                APP_DENIED,
                "legacy activation Authority bindings are incomplete",
            ));
        }
    }
    Ok(())
}

// Hashes limit string comparisons to the candidate bucket. The borrowed
// tuples decide equality, even when different keys produce the same hash.
type Entry<T> = (u64, T, usize);

struct Keys<T> {
    items: Vec<Entry<T>>,
    _capacity: NativeSetupReservation,
}

impl<T: Hash + Eq> Keys<T> {
    fn new<S: OriginalAuthorityOwner>(op: &Operation<'_, S>, count: usize) -> Result<Self> {
        let bytes = count
            .checked_mul(std::mem::size_of::<Entry<T>>())
            .ok_or_else(capacity)?;
        let reservation = op.reserve(GRAPH, bytes)?;
        let mut items = Vec::new();
        items.try_reserve_exact(count).map_err(|_| capacity())?;
        if items.capacity() > count {
            return Err(capacity());
        }
        Ok(Self {
            items,
            _capacity: reservation,
        })
    }

    fn insert<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        (key, units): (T, usize),
    ) -> Result<()> {
        compare_work(op, budget, units + 1)?;
        let mut hasher = DefaultHasher::new();
        key.hash(&mut hasher);
        self.items.push((hasher.finish(), key, units));
        Ok(())
    }

    fn sort<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<()> {
        let levels = Self::search_cost(self.items.len());
        let units = std::mem::size_of::<Entry<T>>()
            .checked_mul(self.items.len())
            .and_then(|bytes| bytes.checked_mul(levels + 1))
            .ok_or_else(capacity)?;
        compare_work(op, budget, units)?;
        self.items.sort_unstable_by_key(|entry| entry.0);
        Ok(())
    }

    fn search_cost(count: usize) -> usize {
        (usize::BITS - count.leading_zeros()) as usize
    }

    fn is_subset_of<S: OriginalAuthorityOwner>(
        &self,
        other: &Self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<bool> {
        for entry in &self.items {
            compare_work(op, budget, Self::search_cost(other.items.len()) + 1)?;
            let start = other.items.partition_point(|item| item.0 < entry.0);
            if !Self::contains_from(&other.items[start..], entry, op, budget)? {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn contains_from<S: OriginalAuthorityOwner>(
        items: &[Entry<T>],
        (hash, key, units): &Entry<T>,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<bool> {
        for entry in items.iter().take_while(|entry| entry.0 == *hash) {
            compare_work(op, budget, *units + 1)?;
            if entry.1 == *key {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

fn capacity() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(GRAPH)
}
