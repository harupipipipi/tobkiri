use async_trait::async_trait;
use std::sync::Arc;
use tobkiri_canon::ids::{RequestId, ShapeRef};
use tobkiri_canon::{AllocationBudget, AllocationError, CoreValue, Digest};
use tobkiri_gate::CallCx;

use crate::StepError;

/// An ordered event bound to the original request and immutable compiled plan.
/// Guest-derived payloads stay in allocation-owned CoreValue graphs.
#[derive(Clone, Debug)]
pub struct RunEvent {
    pub request: RequestId,
    pub plan_digest: Digest,
    pub sequence: u64,
    pub kind: RunEventKind,
}

/// Observable Flow lifecycle, including every provider failure.
#[derive(Clone, Debug)]
pub enum RunEventKind {
    Started,
    StepStarted {
        step: String,
    },
    StepValue {
        step: String,
        value: CoreValue,
    },
    Emitted {
        shape: ShapeRef,
        value: CoreValue,
    },
    StepFailed {
        step: String,
        error: StepError,
        continued: bool,
    },
    Completed {
        value: CoreValue,
        cost_units: u64,
    },
    Cancelled {
        reason: String,
    },
    Failed {
        error: StepError,
    },
}

fn allocation(error: AllocationError) -> StepError {
    StepError::new(
        match error {
            AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth => {
                "limit-exceeded"
            }
            AllocationError::Invalid => "invalid",
            _ => "unavailable",
        },
        "run event allocation failed",
    )
}
fn text(value: &str, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue, StepError> {
    CoreValue::string(value, budget).map_err(allocation)
}
fn signed(value: u64, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue, StepError> {
    let value = i64::try_from(value)
        .map_err(|_| StepError::new("limit-exceeded", "run event integer overflow"))?;
    CoreValue::int(value, budget).map_err(allocation)
}

struct Fields<const N: usize> {
    values: [Option<(&'static str, CoreValue)>; N],
    len: usize,
}
impl<const N: usize> Fields<N> {
    fn new() -> Self {
        Self {
            values: std::array::from_fn(|_| None),
            len: 0,
        }
    }
    fn push(&mut self, key: &'static str, value: CoreValue) -> Result<(), StepError> {
        let slot = self
            .values
            .get_mut(self.len)
            .ok_or_else(|| StepError::new("internal", "run event field bound exceeded"))?;
        *slot = Some((key, value));
        self.len += 1;
        Ok(())
    }
    fn finish(self, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue, StepError> {
        let values = self
            .values
            .into_iter()
            .take(self.len)
            .map(|field| field.ok_or(AllocationError::Invalid));
        CoreValue::object_shared(values, budget).map_err(allocation)
    }
}

impl RunEvent {
    /// Build a canonical-shaped frame without extracting a guest payload into
    /// an ordinary owned JSON value.
    pub fn to_value(&self, budget: &Arc<dyn AllocationBudget>) -> Result<CoreValue, StepError> {
        let mut kind = Fields::<4>::new();
        match &self.kind {
            RunEventKind::Started => kind.push("kind", text("started", budget)?)?,
            RunEventKind::StepStarted { step } => {
                kind.push("kind", text("step_started", budget)?)?;
                kind.push("step", text(step, budget)?)?;
            }
            RunEventKind::StepValue { step, value } => {
                kind.push("kind", text("step_value", budget)?)?;
                kind.push("step", text(step, budget)?)?;
                kind.push("value", value.share())?;
            }
            RunEventKind::Emitted { shape, value } => {
                kind.push("kind", text("emitted", budget)?)?;
                kind.push("shape", text(&shape.to_string(), budget)?)?;
                kind.push("value", value.share())?;
            }
            RunEventKind::StepFailed {
                step,
                error,
                continued,
            } => {
                kind.push("kind", text("step_failed", budget)?)?;
                kind.push("step", text(step, budget)?)?;
                kind.push("error", error_value(error, budget)?)?;
                kind.push(
                    "continued",
                    CoreValue::bool(*continued, budget).map_err(allocation)?,
                )?;
            }
            RunEventKind::Completed { value, cost_units } => {
                kind.push("kind", text("completed", budget)?)?;
                kind.push("value", value.share())?;
                kind.push("cost_units", signed(*cost_units, budget)?)?;
            }
            RunEventKind::Cancelled { .. } => {
                kind.push("kind", text("cancelled", budget)?)?;
                kind.push("reason", text("operation cancelled", budget)?)?;
            }
            RunEventKind::Failed { error } => {
                kind.push("kind", text("failed", budget)?)?;
                kind.push("error", error_value(error, budget)?)?;
            }
        }
        let kind = kind.finish(budget)?;
        let mut frame = Fields::<4>::new();
        frame.push("request", text(&self.request.to_string(), budget)?)?;
        frame.push("plan_digest", text(&self.plan_digest.to_string(), budget)?)?;
        frame.push("sequence", signed(self.sequence, budget)?)?;
        frame.push("kind", kind)?;
        frame.finish(budget)
    }

    /// Parse a frame by borrowed inspection; nested guest values retain their
    /// original allocation owner through CoreValue links.
    pub fn from_value(value: &CoreValue) -> Result<Self, StepError> {
        let request =
            RequestId::parse(&string_field(value, "request")?).map_err(|_| invalid_frame())?;
        let plan_digest =
            Digest::parse(&string_field(value, "plan_digest")?).map_err(|_| invalid_frame())?;
        let sequence = int_field(value, "sequence")?;
        let kind_value = value.get_key("kind").ok_or_else(invalid_frame)?;
        let kind_tag = string_field(&kind_value, "kind")?;
        let kind = match kind_tag.as_str() {
            "started" => RunEventKind::Started,
            "step_started" => RunEventKind::StepStarted {
                step: string_field(&kind_value, "step")?.into(),
            },
            "step_value" => RunEventKind::StepValue {
                step: string_field(&kind_value, "step")?.into(),
                value: kind_value.get_key("value").ok_or_else(invalid_frame)?,
            },
            "emitted" => RunEventKind::Emitted {
                shape: ShapeRef::parse(&string_field(&kind_value, "shape")?)
                    .map_err(|_| invalid_frame())?,
                value: kind_value.get_key("value").ok_or_else(invalid_frame)?,
            },
            "step_failed" => RunEventKind::StepFailed {
                step: string_field(&kind_value, "step")?.into(),
                error: parse_error(&kind_value)?,
                continued: bool_field(&kind_value, "continued")?,
            },
            "completed" => RunEventKind::Completed {
                value: kind_value.get_key("value").ok_or_else(invalid_frame)?,
                cost_units: int_field(&kind_value, "cost_units")?,
            },
            "cancelled" => RunEventKind::Cancelled {
                reason: "operation cancelled".into(),
            },
            "failed" => RunEventKind::Failed {
                error: parse_error(&kind_value)?,
            },
            _ => return Err(invalid_frame()),
        };
        Ok(Self {
            request,
            plan_digest,
            sequence,
            kind,
        })
    }
}

fn error_value(
    error: &StepError,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StepError> {
    let mut fields = Fields::<2>::new();
    let code = error.audit_code();
    fields.push("code", text(code, budget)?)?;
    fields.push("message", text(safe_error_message(code), budget)?)?;
    fields.finish(budget)
}
fn parse_error(value: &CoreValue) -> Result<StepError, StepError> {
    let error = value.get_key("error").ok_or_else(invalid_frame)?;
    let code_value = error.get_key("code").ok_or_else(invalid_frame)?;
    let code = code_value.as_str().ok_or_else(invalid_frame)?;
    let code = match code {
        "denied" => "denied",
        "approval-required" => "approval-required",
        "cancelled" => "cancelled",
        "not-found" => "not-found",
        "invalid" => "invalid",
        "limit-exceeded" => "limit-exceeded",
        "unavailable" => "unavailable",
        "trap" => "trap",
        "blocked" => "blocked",
        "conflict" => "conflict",
        _ => "internal",
    };
    Ok(StepError::new(code, safe_error_message(code)))
}
fn safe_error_message(code: &str) -> &'static str {
    match code {
        "denied" => "operation denied",
        "approval-required" => "operation requires approval",
        "cancelled" => "operation cancelled",
        "not-found" => "requested item not found",
        "invalid" => "operation input is invalid",
        "limit-exceeded" => "execution limit exceeded",
        "unavailable" => "operation is unavailable",
        "trap" => "provider trapped",
        "blocked" => "operation blocked",
        "conflict" => "operation conflicts with current state",
        _ => "internal operation failure",
    }
}
fn string_field(value: &CoreValue, key: &str) -> Result<String, StepError> {
    value
        .get_key(key)
        .and_then(|field| field.as_str().map(str::to_owned))
        .ok_or_else(invalid_frame)
}
fn int_field(value: &CoreValue, key: &str) -> Result<u64, StepError> {
    value
        .get_key(key)
        .and_then(|field| field.as_i64())
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(invalid_frame)
}
fn bool_field(value: &CoreValue, key: &str) -> Result<bool, StepError> {
    value
        .get_key(key)
        .and_then(|field| field.as_bool())
        .ok_or_else(invalid_frame)
}
fn invalid_frame() -> StepError {
    StepError::new("invalid", "invalid run stream frame")
}
#[async_trait]
pub trait RunJournal: Send + Sync {
    async fn append(
        &self,
        cx: &CallCx,
        event: &RunEvent,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError>;
}
