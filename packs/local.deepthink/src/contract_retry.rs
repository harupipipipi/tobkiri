//! A confirmed schema rejection may suspend before another dispatch claim.
//! This receipt is diagnostic preparation, never a serialized dispatch permit.

use super::{AttemptContext, Failure, Run, VERSION, digest, invalid, phase_schema};
use crate::phases;
use serde_json::{Value, json};

pub const RETRY_BOUNDARY_VERSION: &str = "deepthink.contract-retry-boundary.v1";

impl Run {
    /// An implicit replay of the original input cannot consume a saved boundary.
    pub fn requires_explicit_continuation(&self) -> bool {
        matches!(self.status.as_str(), "paused" | "awaiting_retry")
            || (self.status == "quarantined"
                && self
                    .error
                    .as_ref()
                    .is_some_and(|error| error.code == "invalid-phase-contract"))
    }

    /// Verify the saved, unclaimed next epoch and report a non-answer boundary.
    /// A fresh ordinary SQL claim remains mandatory after explicit continuation.
    pub fn retry_receipt(&self) -> Result<Value, Failure> {
        if self.version != VERSION || self.status != "awaiting_retry" {
            return Err(invalid("the workflow has no saved contract retry boundary"));
        }
        self.ensure_generations_reconciled()?;
        let phase = self
            .pending
            .front()
            .ok_or_else(|| invalid("retry has no pending phase"))?;
        let entry = self
            .entry
            .as_ref()
            .ok_or_else(|| invalid("retry has no phase entry"))?;
        if self.error.is_some()
            || self.waiting.is_some()
            || entry.goal != self.goal
            || entry.context != self.context
            || entry.sections != self.sections
            || entry.candidate != self.candidate
            || entry.candidate_state != self.candidate_state
            || json!(entry.pending) != json!(self.pending)
        {
            return Err(invalid("contract retry is not an unsettled phase entry"));
        }
        let point = self
            .last_generation
            .as_ref()
            .ok_or_else(|| invalid("retry lost its rejection"))?;
        let phase_key = format!(
            "{}@{}",
            phase.id,
            self.phase_epochs.get(&phase.id).copied().unwrap_or(0)
        );
        let (base_phase, round) = point
            .base_key
            .rsplit_once(':')
            .ok_or_else(|| invalid("invalid retry phase"))?;
        let round_number = round
            .parse::<u32>()
            .map_err(|_| invalid("invalid retry round"))?;
        if base_phase != phase_key
            || round_number.to_string() != round
            || round_number > self.settings.max_tool_rounds
            || !self.phase_inputs.contains_key(&phase_key)
        {
            return Err(invalid("contract retry changed its captured phase input"));
        }
        let phase_prefix = format!("{phase_key}:");
        if self.tool_receipts.iter().any(|(key, receipt)| {
            key.starts_with(&phase_prefix)
                || receipt.status != "completed"
                || receipt.outcome.is_none()
        }) {
            return Err(Failure {
                code: "needs-reconciliation".into(),
                message:
                    "A contract retry cannot suspend across uncertain or same-phase tool effects."
                        .into(),
            });
        }
        let next = self
            .generation_epochs
            .get(&point.base_key)
            .copied()
            .ok_or_else(|| invalid("retry has no prepared epoch"))?;
        let previous = next
            .checked_sub(1)
            .ok_or_else(|| invalid("retry has no preceding generation"))?;
        let next_key = format!("{}#{next}", point.base_key);
        if point.key != format!("{}#{previous}", point.base_key)
            || self.generations.contains_key(&point.key)
            || self.generations.contains_key(&next_key)
            || self.rejected_generations.contains_key(&next_key)
            || self.attempts.iter().any(|(id, attempt)| {
                attempt
                    .pointer("/context/generation_key")
                    .and_then(Value::as_str)
                    == Some(next_key.as_str())
                    || id.starts_with(&format!("{next_key}:"))
            })
        {
            return Err(invalid("the prepared retry was already used or changed"));
        }
        let rejected = self
            .rejected_generations
            .get(&point.key)
            .ok_or_else(|| invalid("retry has no exact rejected completion"))?;
        let mut originals = self.attempts.iter().filter(|(_, attempt)| {
            attempt
                .pointer("/context/generation_key")
                .and_then(Value::as_str)
                == Some(point.key.as_str())
        });
        let (id, attempt) = originals
            .next()
            .ok_or_else(|| invalid("retry has no original attempt"))?;
        if originals.next().is_some() || self.generation_resolutions.contains_key(id) {
            return Err(invalid(
                "retry has ambiguous or separately resolved original evidence",
            ));
        }
        let context: AttemptContext = serde_json::from_value(attempt["context"].clone())
            .map_err(|_| invalid("invalid retry attempt context"))?;
        if attempt["status"] != "completed"
            || attempt["phase"] != phase.id
            || attempt.get("usage") != Some(&rejected.generated.usage)
            || context.attempt_id != *id
            || context.run_id != self.id
            || context.run_request_digest != self.request_digest
            || context.phase != phase.id
            || context.request_digest != rejected.request_digest
            || context.route_digest != digest(&json!({"model":self.model,"settings":self.settings}))
        {
            return Err(invalid("retry changed its completed attempt or accounting"));
        }
        let count = self
            .contract_retries
            .get(&point.base_key)
            .copied()
            .unwrap_or(0);
        let retry = self
            .retry_contexts
            .get(&next_key)
            .ok_or_else(|| invalid("retry context is missing"))?;
        if count == 0
            || count > self.settings.max_contract_retries
            || retry["failed_generation"] != point.key
            || retry["failure"]["code"] != "invalid-phase-contract"
            || retry["completed_tool_receipts"] != json!(self.tool_receipts)
            || retry["allow_additional_tools"] != true
            || retry["phase_contract"] != phase_schema(&phase.id)
            || retry["phase_execution_policy"] != phases::execution_policy(&phase.id)
            || retry["retry_boundary"] != json!({"version":RETRY_BOUNDARY_VERSION,"retry":count})
        {
            return Err(invalid(
                "retry counter, captured contract or preparation marker changed",
            ));
        }
        Ok(json!({
            "version":VERSION,"retry_boundary_version":RETRY_BOUNDARY_VERSION,
            "run_id":self.id,"conversation":self.conversation,"status":"awaiting_retry","approved":false,
            "phase":phase.id,"next_phase":phase.id,"completed_phases":self.phases.len(),
            "completed_phases_this_segment":0,"reason":"invalid_phase_contract",
            "rejected_attempt":id,"rejected_generation":point.key,"prepared_generation":next_key,
            "retry":count,"maximum_retries":self.settings.max_contract_retries,"usage":self.usage()
        }))
    }
}
