//! Match the restored activation records while the App activation lock is held.
use super::super::active_reservation;
use super::*;
use crate::original_authority::io::{Store, incomplete_reservations};
use rusqlite::types::Value;
use tobkiri_canon::{AllocationKind, AllocationRequest};

#[path = "original_recovery_snapshot_graph_current.rs"]
mod current;
#[path = "original_recovery_snapshot_graph_legacy.rs"]
mod legacy;
#[path = "original_recovery_snapshot_projections.rs"]
mod projections;
#[path = "original_recovery_legacy_successor.rs"]
mod successor;
pub(super) use legacy::resolver_input;
pub(super) use successor::verify as verify_legacy_successor;

const GRAPH: OriginalAuthorityStage = OriginalAuthorityStage::Source;

#[derive(Clone, Copy)]
pub(super) struct View<'a> {
    pub(super) doc: &'a Document,
    pub(super) id: usize,
}

impl<'a> View<'a> {
    pub(super) fn at(self, id: usize) -> Self {
        Self { id, ..self }
    }

    fn field(self, key: &'static str) -> Result<Self> {
        Ok(self.at(subscript(self.doc, self.id, key)?))
    }

    fn get(self, key: &str) -> Option<Self> {
        self.doc.get(self.id, key).map(|id| self.at(id))
    }

    fn node(self) -> &'a Node {
        self.doc.node(self.id)
    }

    fn items(self) -> Result<&'a [usize]> {
        match self.node() {
            Node::Array(items) => Ok(items),
            _ => Err(OriginalAuthorityError::unsupported(
                GRAPH,
                "original activation record list",
            )),
        }
    }

    fn text(self) -> Result<&'a Text> {
        match self.node() {
            Node::String(text) => Ok(text),
            _ => Err(OriginalAuthorityError::unsupported(
                GRAPH,
                "original activation record text",
            )),
        }
    }

    fn is(self, expected: &str) -> bool {
        self.doc.string_is(self.id, expected)
    }

    fn canonical<S: OriginalAuthorityOwner>(
        self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Funded<Vec<u8>>> {
        json::canonical_subtree(self.doc, self.id, None, op, budget)
    }

    fn digest<S: OriginalAuthorityOwner>(
        self,
        omit: Option<&str>,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Digest> {
        let bytes = json::canonical_subtree(self.doc, self.id, omit, op, budget)?;
        Ok(Digest::hash(&bytes))
    }

    fn same<S: OriginalAuthorityOwner>(
        self,
        other: View<'_>,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<bool> {
        if std::ptr::eq(self.doc, other.doc) && self.id == other.id {
            return Ok(true);
        }
        compare_work(op, budget, 1)?;
        match (self.node(), other.node()) {
            (Node::Scalar(CanonValue::Int(number)), Node::Scalar(CanonValue::Bool(flag)))
            | (Node::Scalar(CanonValue::Bool(flag)), Node::Scalar(CanonValue::Int(number))) => {
                Ok(*number == i64::from(*flag))
            }
            (Node::Scalar(left), Node::Scalar(right)) => Ok(left == right),
            (Node::String(left), Node::String(right)) => equal_text(left, right, op, budget),
            (Node::Array(left), Node::Array(right)) if left.len() == right.len() => {
                for (&a, &b) in left.iter().zip(right) {
                    if !self.at(a).same(other.at(b), op, budget)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            (Node::Object(left), Node::Object(right)) if left.len() == right.len() => {
                for ((key, child), (key_at_index, child_at_index)) in left.iter().zip(right) {
                    let mut counterpart = *child_at_index;
                    if !equal_text(key, key_at_index, op, budget)? {
                        let mut found = None;
                        for (name, value) in right {
                            if equal_text(key, name, op, budget)? {
                                found = Some(*value);
                                break;
                            }
                        }
                        let Some(value) = found else {
                            return Ok(false);
                        };
                        counterpart = value;
                    }
                    if !self.at(*child).same(other.at(counterpart), op, budget)? {
                        return Ok(false);
                    }
                }
                Ok(true)
            }
            _ => Ok(false),
        }
    }
}

fn equal_text<S: OriginalAuthorityOwner>(
    left: &Text,
    right: &Text,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<bool> {
    let extent = if left.0.len() == right.0.len() {
        left.0.len()
    } else {
        0
    };
    compare_work(op, budget, extent + 1)?;
    Ok(left == right)
}

fn compare_work<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    work: usize,
) -> Result<()> {
    op.check(GRAPH)?;
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            depth: 0,
            elements: 0,
            work: work as u64,
        })
        .map_err(|_| OriginalAuthorityError::capacity(GRAPH))
}

#[derive(Clone, Copy)]
pub(super) struct ResolvedRecords<'a> {
    pub(super) profile: View<'a>,
    pub(super) lock: View<'a>,
    pub(super) plan: View<'a>,
}

#[derive(Clone, Copy)]
pub(super) struct SnapshotRecords<'a> {
    pub(super) profile: View<'a>,
    pub(super) lock: View<'a>,
    pub(super) plan: View<'a>,
    pub(super) activation: View<'a>,
}

#[derive(Clone, Copy)]
pub(super) struct Snapshot<'a> {
    pub(super) records: SnapshotRecords<'a>,
    pub(super) pointer: View<'a>,
    pub(super) legacy: bool,
}

pub(super) fn verify<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    budget: &Arc<dyn AllocationBudget>,
    snapshot: Snapshot<'_>,
    profile_id: &str,
) -> Result<ProfileCheck> {
    let records = snapshot.records;
    if snapshot.legacy {
        legacy::verify(
            op,
            budget,
            records,
            profile_id,
            |expected| epoch_matches(op, store, expected),
            |activation| reservation_matches(op, store, budget, activation),
        )?;
        return Ok(ProfileCheck::NeedsLegacySuccessor);
    }
    verify_resolved(
        op,
        budget,
        ResolvedRecords {
            profile: records.profile,
            lock: records.lock,
            plan: records.plan,
        },
    )?;
    if !records.activation.field("activation_id")?.same(
        snapshot.pointer.field("activation_id")?,
        op,
        budget,
    )? {
        return Err(raw(APP_DENIED, "active pointer selects another activation"));
    }
    if !records.activation.field("state")?.is("active") {
        return Err(raw(APP_DENIED, "activation is stale or not active"));
    }
    for (field, record) in [
        ("profile_revision", records.plan),
        ("catalog_revision", records.plan),
        ("bundle_digest", records.plan),
        ("lock_digest", records.lock),
        ("plan_digest", records.plan),
        ("closure_digest", records.plan),
    ] {
        if !records
            .activation
            .field(field)?
            .same(record.field(field)?, op, budget)?
        {
            return Err(raw(APP_DENIED, "activation is stale or not active"));
        }
    }
    if !reservation_matches(op, store, budget, records.activation)?
        || !epoch_matches(op, store, records.activation.field("security_epoch")?)?
    {
        return Err(raw(
            APP_DENIED,
            "active activation authority, fence, or SecurityEpoch is stale",
        ));
    }
    Ok(ProfileCheck::NeedsArtifactReverification)
}

/// Validate the resolved graph independently of an activation or reservation.
pub(super) fn verify_resolved<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    records: ResolvedRecords<'_>,
) -> Result<()> {
    current::verify(op, budget, records, |selections, effective_set| {
        projections::verify(op, budget, selections, effective_set)
    })
}

/// Retain the canonical record identities and data consumed after unlocking.
/// Only registered Setup needs the complete Profile and Plan for its policy.
pub(super) fn retain_active<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    records: SnapshotRecords<'_>,
    projection: SnapshotProjection,
) -> Result<ActiveSnapshot> {
    let capacity = || OriginalAuthorityError::capacity(GRAPH);
    let to_core = |value: View<'_>| {
        let bytes = value.canonical(op, budget)?;
        CoreValue::parse_guest(&bytes, budget).map_err(|_| capacity())
    };
    let verified_digest = |record: View<'_>, field| {
        let value = record.field(field)?;
        let rendered = definitions::render_python_str(op, budget, value.doc, value.id)?;
        Digest::parse(rendered.as_str().expect("strict digest text"))
            .map_err(|_| OriginalAuthorityError::unsupported(GRAPH, "verified activation digest"))
    };
    // The graph already checked these three seals against the exact records.
    let seals = [
        verified_digest(records.plan, "profile_revision")?,
        verified_digest(records.lock, "lock_digest")?,
        verified_digest(records.plan, "plan_digest")?,
        records.activation.digest(None, op, budget)?,
    ];
    let review_records = match projection {
        SnapshotProjection::Identity => None,
        SnapshotProjection::RegisteredReview => Some(ActiveReviewRecords {
            profile: to_core(records.profile)?,
            plan: to_core(records.plan)?,
        }),
    };
    let artifact_profile = if let Some(review) = &review_records {
        review.profile.share()
    } else {
        retain_artifact_profile(op, budget, records.profile)?
    };
    let candidate = CoreValue::object_shared(
        [
            Ok(("profile_id", to_core(records.profile.field("profile_id")?)?)),
            Ok((
                "profile_revision",
                to_core(records.plan.field("profile_revision")?)?,
            )),
            Ok((
                "activation_id",
                to_core(records.activation.field("activation_id")?)?,
            )),
            Ok(("plan_digest", to_core(records.plan.field("plan_digest")?)?)),
            Ok(("lock_digest", to_core(records.lock.field("lock_digest")?)?)),
            Ok((
                "profile_definition_digest",
                to_core(records.plan.field("profile_definition_digest")?)?,
            )),
            Ok((
                "security_epoch",
                to_core(records.plan.field("security_epoch")?)?,
            )),
        ]
        .into_iter(),
        budget,
    )
    .map_err(|_| capacity())?;
    Ok(ActiveSnapshot {
        artifact_profile,
        review_records,
        activation_profile_id: to_core(records.activation.field("profile_id")?)?,
        catalog_revision: to_core(records.plan.field("catalog_revision")?)?,
        records: seals,
        candidate,
    })
}

fn retain_artifact_profile<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    profile: View<'_>,
) -> Result<CoreValue> {
    let shell = profile.field("shell")?.canonical(op, budget)?;
    let shell = CoreValue::parse_guest(&shell, budget)
        .map_err(|_| OriginalAuthorityError::capacity(GRAPH))?;
    CoreValue::object_shared(std::iter::once(Ok(("shell", shell))), budget)
        .map_err(|_| OriginalAuthorityError::capacity(GRAPH))
}

fn epoch_matches<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    expected: View<'_>,
) -> Result<bool> {
    let epoch = incomplete_reservations::read_epoch(op, store)?;
    Ok(
        matches!(expected.node(), Node::Scalar(CanonValue::Int(number))
        if epoch.decimal().parse::<i64>() == Ok(*number)),
    )
}

fn reservation_matches<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    budget: &Arc<dyn AllocationBudget>,
    activation: View<'_>,
) -> Result<bool> {
    let id = activation.field("activation_id")?;
    let id = definitions::render_python_str(op, budget, id.doc, id.id)?;
    let row = active_reservation::read_snapshot_binding(
        op,
        store,
        id.as_str().expect("strict JSON activation ID"),
    )?;
    // The original assembles the expected tuple even when no row exists.
    let expected = [
        activation.field("profile_id")?,
        activation.field("plan_digest")?,
        activation.field("profile_authority_snapshot_digest")?,
        activation.field("security_epoch")?,
        activation.field("fencing_token")?,
    ];
    Ok(row.is_some_and(|row| {
        row.iter().zip(expected).all(|(actual, expected)| {
            match (actual.as_ref(), expected.node()) {
                (Some(Value::Text(actual)), Node::String(expected)) => expected.is(actual),
                (Some(Value::Integer(actual)), Node::Scalar(CanonValue::Int(expected))) => {
                    actual == expected
                }
                (Some(Value::Real(actual)), Node::Scalar(CanonValue::Int(expected))) => {
                    // The canonical record's integers lie in the I-JSON safe range.
                    *actual == *expected as f64
                }
                _ => false,
            }
        })
    }))
}
