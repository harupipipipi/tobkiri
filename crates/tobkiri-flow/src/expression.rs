//! Pure wrapper around the allocation-owned Flow expression evaluator.
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, CoreValue};

use crate::{StepError, owned_expression::boolean_owned};

pub type Environment = crate::RuntimeEnvironment;

pub fn evaluate(
    value: &tobkiri_canon::CanonValue,
    env: &Environment,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StepError> {
    crate::evaluate_owned(value, env, budget)
}

pub(crate) fn boolean(value: &CoreValue) -> Result<bool, StepError> {
    boolean_owned(value)
}
