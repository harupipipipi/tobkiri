//! Select optional roots from the verified active Profile and the review catalog.
use super::*;
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreArray};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

struct Selection {
    id: CoreValue,
    covered: bool,
}

impl Selection {
    fn id(&self) -> &str {
        self.id.as_str().expect("verified Pack ID")
    }
}

impl PublicationRead {
    /// Find previously selected roots whose approvals must be rechecked.
    pub(crate) fn review_registered_optional_roots<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        loader: &Mutex<Option<CatalogIo>>,
        active_profile: &CoreValue,
        candidate: &CoreValue,
    ) -> Result<CoreValue> {
        let op = self.candidate_operation(owner, cancellation)?;
        let io = loader.lock().unwrap_or_else(|error| error.into_inner());
        let io = io
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        io.require_recovery_catalog(&op)?;
        select(&op, budget, active_profile, candidate, |id| {
            io.bundled_review_dependencies(id)
        })
    }
}

fn select<'catalog, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    active_profile: &CoreValue,
    candidate: &CoreValue,
    dependencies: impl Fn(&str) -> Result<&'catalog CoreValue>,
) -> Result<CoreValue> {
    op.check(STAGE)?;
    let active_packs = field(active_profile, "packs")?;
    let active_packs = rows(&active_packs)?;
    let bytes = active_packs
        .len()
        .checked_mul(size_of::<Selection>())
        .ok_or_else(|| capacity(()))?;
    let _funding = op.reserve(STAGE, bytes)?;
    let mut selected = Vec::new();
    selected
        .try_reserve_exact(active_packs.len())
        .map_err(capacity)?;
    if selected.capacity() != active_packs.len() {
        return Err(capacity(()));
    }
    for row in active_packs.iter() {
        op.check(STAGE)?;
        selected.push(Selection {
            id: pack_id(&row)?,
            covered: false,
        });
    }
    let max_len = selected
        .iter()
        .map(|pack| pack.id().len())
        .max()
        .unwrap_or(0);
    charge(
        budget,
        selected
            .len()
            .saturating_mul(usize::BITS as usize)
            .saturating_mul(max_len + 1),
    )?;
    selected.sort_unstable_by(|left, right| left.id().cmp(right.id()));
    selected.dedup_by(|left, right| left.id() == right.id());

    for row in rows(&field(candidate, "packs")?)?.iter() {
        op.check(STAGE)?;
        let id = pack_id(&row)?;
        mark_covered(
            &mut selected,
            id.as_str().expect("verified Pack ID"),
            budget,
        )?;
    }

    for index in 0..selected.len() {
        op.check(STAGE)?;
        let deps = dependencies(selected[index].id())?;
        mark_dependencies(&mut selected, rows(deps)?, op, budget)?;
    }
    let base = pack_id(&field(active_profile, "base")?)?;
    let shell = pack_id(&field(active_profile, "shell")?)?;
    let base = base.as_str().expect("verified Pack ID");
    let shell = shell.as_str().expect("verified Pack ID");
    for id in [Some(base), (shell != base).then_some(shell)]
        .into_iter()
        .flatten()
    {
        op.check(STAGE)?;
        if index_of(&selected, id, budget)?.is_none() {
            mark_dependencies(&mut selected, rows(dependencies(id)?)?, op, budget)?;
        }
    }

    let count = selected.iter().filter(|pack| !pack.covered).count();
    let mut roots = CoreValue::array_builder(budget, count).map_err(capacity)?;
    for pack in selected.into_iter().filter(|pack| !pack.covered) {
        roots.push(pack.id).map_err(capacity)?;
    }
    roots.finish().map_err(capacity)
}

fn mark_dependencies<S: OriginalAuthorityOwner>(
    packs: &mut [Selection],
    dependencies: CoreArray<'_>,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    for id in dependencies.iter() {
        op.check(STAGE)?;
        mark_covered(
            packs,
            id.as_str()
                .ok_or_else(OriginalAuthorityError::unavailable)?,
            budget,
        )?;
    }
    Ok(())
}

fn mark_covered(
    packs: &mut [Selection],
    id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    if let Some(index) = index_of(packs, id, budget)? {
        packs[index].covered = true;
    }
    Ok(())
}

fn index_of(
    packs: &[Selection],
    id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<usize>> {
    let levels = usize::BITS - packs.len().leading_zeros();
    charge(budget, (levels as usize + 1).saturating_mul(id.len() + 1))?;
    Ok(packs.binary_search_by(|pack| pack.id().cmp(id)).ok())
}

fn field(value: &CoreValue, key: &str) -> Result<CoreValue> {
    value
        .get_key(key)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn rows(value: &CoreValue) -> Result<CoreArray<'_>> {
    value
        .array()
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn pack_id(value: &CoreValue) -> Result<CoreValue> {
    let id = field(value, "pack_id")?;
    id.as_str()
        .is_some()
        .then_some(id)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn charge(budget: &Arc<dyn AllocationBudget>, work: usize) -> Result<()> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            depth: 0,
            elements: 0,
            work: work as u64,
        })
        .map_err(capacity)
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    fn parse(value: Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
        CoreValue::parse_guest(&serde_json::to_vec(&value).unwrap(), budget).unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn optional_roots_exclude_declared_packs_and_every_selected_dependency() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let active = parse(
                json!({
                    "packs":[{"pack_id":"z"},{"pack_id":"declared"},{"pack_id":"a"},
                             {"pack_id":"base-dep"},{"pack_id":"shell-dep"},{"pack_id":"a-dep"},
                             {"pack_id":"newly-declared"},{"pack_id":"a"}],
                    "base":{"pack_id":"base"}, "shell":{"pack_id":"shell"},
                }),
                budget,
            );
            let candidate = parse(
                json!({"packs":[{"pack_id":"newly-declared"},{"pack_id":"declared"}]}),
                budget,
            );
            let base_deps = parse(json!(["base-dep"]), budget);
            let shell_deps = parse(json!(["shell-dep"]), budget);
            let a_deps = parse(json!(["a-dep"]), budget);
            let none = parse(json!([]), budget);
            let result = select(&op, budget, &active, &candidate, |id| {
                Ok(match id {
                    "base" => &base_deps,
                    "shell" => &shell_deps,
                    "a" => &a_deps,
                    _ => &none,
                })
            })
            .unwrap();
            assert_eq!(
                result.encode_guest(budget).unwrap().as_bytes(),
                br#"["a","z"]"#
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn all_declared_still_requires_the_verified_dependency_inventory() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let profile = parse(
                json!({"packs":[{"pack_id":"p"}], "base":{"pack_id":"base"},
                                      "shell":{"pack_id":"shell"}}),
                budget,
            );
            let none = parse(json!([]), budget);
            assert!(
                select(&op, budget, &profile, &profile, |id| {
                    if id == "shell" {
                        Err(OriginalAuthorityError::unavailable())
                    } else {
                        Ok(&none)
                    }
                })
                .is_err()
            );
            let result = select(&op, budget, &profile, &profile, |_| Ok(&none)).unwrap();
            assert!(result.array().unwrap().is_empty());
        });
    }
}
