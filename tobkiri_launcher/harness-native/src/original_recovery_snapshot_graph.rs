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
    current::verify(op, budget, records, |selections, effective_set| {
        projections::verify(op, budget, selections, effective_set)
    })?;
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
