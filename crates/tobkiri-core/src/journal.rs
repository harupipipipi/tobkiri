use async_trait::async_trait;
use std::{collections::BTreeMap, sync::Arc};
use tobkiri_canon::{AllocationBudget, CanonValue, Digest};
use tobkiri_flow::{RunEvent, RunEventKind, RunJournal, StepError};
use tobkiri_gate::{AuditEvent, AuditSink, CallCx};

/// Persist run transitions to the Core's hash-chained audit writer. Values are
/// represented by digests; guest values and secret plaintext are never logged.
pub struct AuditRunJournal(pub Arc<dyn AuditSink>);

#[async_trait]
impl RunJournal for AuditRunJournal {
    async fn append(
        &self,
        cx: &CallCx,
        event: &RunEvent,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError> {
        let mut fields = BTreeMap::from([
            (
                "plan_digest".into(),
                CanonValue::Str(event.plan_digest.to_string()),
            ),
            (
                "sequence".into(),
                CanonValue::Int(event.sequence.try_into().map_err(|_| {
                    StepError::new("limit-exceeded", "run journal sequence overflow")
                })?),
            ),
            (
                "run_request".into(),
                CanonValue::Str(event.request.to_string()),
            ),
        ]);
        let mut value_digest = None;
        let kind = match &event.kind {
            RunEventKind::Started => "run-started",
            RunEventKind::StepStarted { step } => {
                fields.insert("step".into(), CanonValue::Str(step.clone()));
                "step-started"
            }
            RunEventKind::StepValue { step, value: v } => {
                fields.insert("step".into(), CanonValue::Str(step.clone()));
                value_digest = Some(v);
                "step-value"
            }
            RunEventKind::Emitted { shape, value: v } => {
                fields.insert("shape".into(), CanonValue::Str(shape.to_string()));
                value_digest = Some(v);
                "run-emitted"
            }
            RunEventKind::StepFailed {
                step,
                error,
                continued,
            } => {
                fields.insert("step".into(), CanonValue::Str(step.clone()));
                fields.insert("code".into(), CanonValue::Str(error.audit_code().into()));
                fields.insert("continued".into(), CanonValue::Bool(*continued));
                "step-failed"
            }
            RunEventKind::Completed {
                value: v,
                cost_units,
            } => {
                value_digest = Some(v);
                fields.insert("cost_units".into(), CanonValue::Str(cost_units.to_string()));
                "run-completed"
            }
            RunEventKind::Cancelled { .. } => "run-cancelled",
            RunEventKind::Failed { error } => {
                fields.insert("code".into(), CanonValue::Str(error.audit_code().into()));
                "run-failed"
            }
        };
        if let Some(value) = value_digest {
            let encoded = value
                .encode_guest(budget)
                .map_err(|_| StepError::new("limit-exceeded", "run payload encoding failed"))?;
            fields.insert(
                "value_digest".into(),
                CanonValue::Str(Digest::hash(encoded.as_bytes()).to_string()),
            );
        }
        self.0
            .append(AuditEvent {
                profile: cx.profile().clone(),
                principal: cx.principal().clone(),
                request: event.request.clone(),
                kind: kind.into(),
                payload: CanonValue::Obj(fields),
            })
            .map_err(|e| StepError::new("internal", e.to_string()))
    }
}
