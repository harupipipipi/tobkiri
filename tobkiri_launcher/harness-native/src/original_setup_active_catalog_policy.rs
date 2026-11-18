//! Decide whether registered Setup can use the verified bundled Pack catalog.
use super::*;
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreRef};

#[path = "original_setup_source_predecessor.rs"]
mod source_predecessor;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const CONFLICT: &str = "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict";

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum RegisteredCatalogSelection {
    Bundled { binding_renewal_required: bool },
    AdmittedClosure,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RegisteredDefinitionSelection {
    Current,
    InterruptedSourcePredecessor,
    PackPinPredecessor,
}

impl PublicationRead {
    /// Compare the verified active selection with the same catalog loaded for review.
    pub(crate) fn review_registered_catalog<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        loader: &Mutex<Option<CatalogIo>>,
        records: &RegisteredCatalogPolicy,
    ) -> Result<RegisteredCatalogSelection> {
        let op = self.candidate_operation(owner, cancellation)?;
        let io = loader.lock().unwrap_or_else(|error| error.into_inner());
        let io = io
            .as_ref()
            .filter(|io| io.completed())
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        select(
            &op,
            budget,
            &records.registered_profile,
            &records.active_profile,
            records.active_plan.as_ref(),
            |id| io.bundled_review_pack_digest(id),
        )
    }

    /// Authenticate the definition base before any change to the candidate catalog.
    pub(crate) fn review_registered_definition<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        loader: &Mutex<Option<CatalogIo>>,
        records: &RegisteredCatalogPolicy,
        include_source_additions: bool,
    ) -> Result<RegisteredDefinitionSelection> {
        let op = self.candidate_operation(owner, cancellation)?;
        op.check(STAGE)?;
        let current_digest = records
            .registered_profile
            .digest_guest(budget)
            .map_err(capacity)?;
        let verified_digest = records
            .verified_definition_digest
            .as_str()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if current_digest.as_str() == verified_digest {
            return Ok(RegisteredDefinitionSelection::Current);
        }

        let (root, candidate) = self.registered_candidate();
        let profile_id = candidate
            .workspace
            .file_name()
            .and_then(|name| name.to_str())
            .expect("verified workspace Profile ID");
        let predecessor = candidate.registered.review_source_predecessor(
            &op,
            root,
            &current_digest,
            verified_digest,
            budget,
        )?;
        // As in the original call, the source lookup follows the independent
        // registry snapshot and uses the catalog loaded for this invocation.
        let source = loader
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .bundled_review_profile(&op, profile_id)?;
        if let Some(predecessor) = predecessor
            && source_predecessor::matches(
                &op,
                &predecessor,
                &source,
                &current_digest,
                records.active_plan.is_none(),
                budget,
            )?
        {
            return Ok(RegisteredDefinitionSelection::InterruptedSourcePredecessor);
        }
        if include_source_additions {
            return Ok(RegisteredDefinitionSelection::PackPinPredecessor);
        }
        Err(raw(
            CONFLICT,
            "bootstrap review does not match a verified interrupted source update",
        ))
    }
}

fn select<'catalog, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    registered_profile: &CoreValue,
    active_profile: &CoreValue,
    active_plan: Option<&CoreValue>,
    bundled: impl Fn(&str) -> Result<Option<&'catalog str>>,
) -> Result<RegisteredCatalogSelection> {
    op.check(STAGE)?;
    let selected = field(active_profile, "packs")?;
    let selected = selected
        .array()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let registered = field(registered_profile, "packs")?;
    let registered = registered
        .array()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let root_count = selected.len().checked_add(2).ok_or_else(|| capacity(()))?;
    let root_bytes = root_count
        .checked_mul(size_of::<CoreValue>())
        .ok_or_else(|| capacity(()))?;
    let pin_bytes = registered
        .len()
        .checked_mul(size_of::<(CoreValue, CoreValue)>())
        .ok_or_else(|| capacity(()))?;
    let _storage = op.reserve(
        STAGE,
        root_bytes
            .checked_add(pin_bytes)
            .ok_or_else(|| capacity(()))?,
    )?;
    let mut roots = Vec::new();
    roots.try_reserve_exact(root_count).map_err(capacity)?;
    let mut pins: Vec<(CoreValue, CoreValue)> = Vec::new();
    pins.try_reserve_exact(registered.len()).map_err(capacity)?;
    if roots.capacity() != root_count || pins.capacity() != registered.len() {
        return Err(capacity(()));
    }
    for row in selected.iter() {
        op.check(STAGE)?;
        roots.push(text(&row, "pack_id")?);
    }
    // Base and Shell use verified selections. The registered definition only
    // contributes exact non-null pins from its declared Pack rows.
    for role in ["base", "shell"] {
        roots.push(text(&field(active_profile, role)?, "pack_id")?);
    }
    let maximum_id_len = roots
        .iter()
        .filter_map(CoreValue::as_str)
        .map(str::len)
        .max()
        .unwrap_or(0);
    charge(
        budget,
        roots
            .len()
            .saturating_mul(usize::BITS as usize)
            .saturating_mul(maximum_id_len + 1),
    )?;
    roots.sort_unstable_by(|left, right| left.as_str().cmp(&right.as_str()));
    roots.dedup_by(|left, right| left.as_str() == right.as_str());
    for row in registered.iter() {
        op.check(STAGE)?;
        let Some(digest) = row.get_key("artifact_digest") else {
            continue;
        };
        if matches!(digest.inspect(), CoreRef::Null) {
            continue;
        }
        if digest.as_str().is_none() {
            return Err(OriginalAuthorityError::unavailable());
        }
        let id = text(&row, "pack_id")?;
        charge(
            budget,
            (id.as_str().expect("checked Pack ID").len() + 1).saturating_mul(pins.len() + 1),
        )?;
        // The original comprehension keeps the last exact row for each ID.
        if let Some((_, previous)) = pins
            .iter_mut()
            .find(|(candidate, _)| candidate.as_str() == id.as_str())
        {
            *previous = digest;
        } else {
            pins.push((id, digest));
        }
    }
    // Missing verified selections require admission even without explicit pins.
    for id in roots {
        op.check(STAGE)?;
        if bundled(id.as_str().expect("checked Pack ID"))?.is_none() {
            return Ok(RegisteredCatalogSelection::AdmittedClosure);
        }
    }
    // Even an ID already in the bundle needs the exact closure path when its
    // registered digest differs. That path needs the full pin map and all
    // admission checks, including the explicit bundled collision exception.
    for (id, digest) in pins {
        op.check(STAGE)?;
        if bundled(id.as_str().expect("checked Pack ID"))?
            .is_some_and(|current| Some(current) != digest.as_str())
        {
            return Ok(RegisteredCatalogSelection::AdmittedClosure);
        }
    }
    let binding_renewal_required = match active_plan {
        Some(plan) => binding_renewal(op, plan, bundled)?,
        None => false,
    };
    Ok(RegisteredCatalogSelection::Bundled {
        binding_renewal_required,
    })
}

fn binding_renewal<'catalog, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    plan: &CoreValue,
    bundled: impl Fn(&str) -> Result<Option<&'catalog str>>,
) -> Result<bool> {
    let bindings = plan.get_key("bindings").ok_or_else(|| {
        raw(
            CONFLICT,
            "artifact binding renewal requires a verified active plan",
        )
    })?;
    let bindings = bindings.array().ok_or_else(|| {
        raw(
            CONFLICT,
            "artifact binding renewal requires a verified active plan",
        )
    })?;
    let mut renewal_required = false;
    for binding in bindings.iter() {
        op.check(STAGE)?;
        if binding.object().is_none() {
            return Err(raw(
                CONFLICT,
                "artifact binding renewal contains an invalid binding",
            ));
        }
        let invalid = || {
            raw(
                CONFLICT,
                "artifact binding renewal cannot verify the selected Pack",
            )
        };
        let id = text(&binding, "pack_id").map_err(|_| invalid())?;
        let current = bundled(id.as_str().expect("checked Pack ID"))?;
        let digest = text(&binding, "artifact_digest").map_err(|_| invalid())?;
        renewal_required |=
            current.ok_or_else(invalid)? != digest.as_str().expect("checked digest");
    }
    Ok(renewal_required)
}

fn field(value: &CoreValue, key: &str) -> Result<CoreValue> {
    value
        .get_key(key)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn text(value: &CoreValue, key: &str) -> Result<CoreValue> {
    let value = field(value, key)?;
    value
        .as_str()
        .is_some()
        .then_some(value)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn charge(budget: &Arc<dyn AllocationBudget>, work: usize) -> Result<()> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
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
    use std::cell::RefCell;

    const CURRENT: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
    const OLD: &str = "sha256:8888888888888888888888888888888888888888888888888888888888888888";

    fn parse(value: &Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
        CoreValue::parse_guest(&serde_json::to_vec(value).unwrap(), budget).unwrap()
    }

    fn active_profile() -> Value {
        json!({
            "packs": [{"pack_id": "gateway"}, {"pack_id": "base"}],
            "base": {"pack_id": "base"},
            "shell": {"pack_id": "shell"}
        })
    }

    #[tokio::test(flavor = "current_thread")]
    async fn verified_closure_requires_admission_for_missing_selections_and_bundled_pin_changes() {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let cases = [
                (None, json!(CURRENT), false, false),
                (None, json!(OLD), false, true),
                (None, Value::Null, false, false),
                (None, Value::Null, true, true),
                (Some("gateway"), Value::Null, false, true),
                (Some("base"), Value::Null, false, true),
                (Some("shell"), Value::Null, false, true),
            ];
            for (missing, digest, registered_only_is_bundled, admitted) in cases {
                let checked = RefCell::new(Vec::new());
                let registered = parse(
                    &json!({
                        "packs": [
                            {"pack_id": "gateway", "artifact_digest": digest},
                            {"pack_id": "unused", "artifact_digest": OLD}
                        ]
                    }),
                    budget,
                );
                let check = |id: &str| {
                    checked.borrow_mut().push(id.to_owned());
                    Ok(
                        (missing != Some(id) && (id != "unused" || registered_only_is_bundled))
                            .then_some(CURRENT),
                    )
                };
                let selected = select(
                    &op,
                    budget,
                    &registered,
                    &parse(&active_profile(), budget),
                    None,
                    check,
                )
                .unwrap();
                assert_eq!(
                    selected,
                    if admitted {
                        RegisteredCatalogSelection::AdmittedClosure
                    } else {
                        RegisteredCatalogSelection::Bundled {
                            binding_renewal_required: false,
                        }
                    }
                );
                let roots = ["base", "gateway", "shell"];
                let compared = missing.map_or(roots.len(), |id| {
                    roots.iter().position(|root| *root == id).unwrap() + 1
                });
                assert_eq!(&checked.borrow()[..compared], &roots[..compared]);
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn bundled_binding_renewal_checks_every_binding_and_skips_the_old_plan_for_shell_successors()
     {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let registered = parse(&json!({"packs": []}), budget);
            let active = parse(&active_profile(), budget);
            let bundled = |id: &str| Ok((id != "unavailable").then_some(CURRENT));
            for (digest, binding_renewal_required) in [(CURRENT, false), (OLD, true)] {
                let plan = parse(
                    &json!({"bindings": [{"pack_id": "gateway", "artifact_digest": digest}]}),
                    budget,
                );
                assert_eq!(
                    select(&op, budget, &registered, &active, Some(&plan), bundled).unwrap(),
                    RegisteredCatalogSelection::Bundled {
                        binding_renewal_required
                    }
                );
            }
            let plan = parse(
                &json!({"bindings": [
                    {"pack_id": "gateway", "artifact_digest": OLD},
                    {"pack_id": "unavailable", "artifact_digest": OLD}
                ]}),
                budget,
            );
            let error =
                select(&op, budget, &registered, &active, Some(&plan), bundled).unwrap_err();
            assert_eq!(error.class, CONFLICT);
            assert_eq!(
                error.message,
                "artifact binding renewal cannot verify the selected Pack"
            );
            assert_eq!(
                select(&op, budget, &registered, &active, None, bundled).unwrap(),
                RegisteredCatalogSelection::Bundled {
                    binding_renewal_required: false
                }
            );
        });
    }
}
