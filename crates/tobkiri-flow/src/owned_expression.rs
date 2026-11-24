//! Runtime expression evaluation over allocation-owned guest graphs.
//! The AST is trusted compiled metadata; runtime references never become CanonValue.
use crate::StepError;
use std::{cell::RefCell, collections::BTreeMap, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CanonValue, CoreRef,
    CoreValue, MAX_DEPTH,
};

/// Persistent allocation-owned environment. Each binding is an immutable
/// three-item node `[name, value, previous]`; shadowing retains the old graph
/// and every value keeps its original owner lease.
#[derive(Clone)]
pub struct RuntimeEnvironment {
    head: CoreValue,
    budget: Arc<dyn AllocationBudget>,
}
impl RuntimeEnvironment {
    pub fn empty(budget: &Arc<dyn AllocationBudget>) -> Result<Self, StepError> {
        CoreValue::null(budget)
            .map(|head| Self {
                head,
                budget: budget.clone(),
            })
            .map_err(allocation)
    }
    pub fn with_input(
        value: CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, StepError> {
        let mut environment = Self::empty(budget)?;
        environment.insert("input", value, budget)?;
        Ok(environment)
    }
    pub fn insert(
        &mut self,
        name: &str,
        value: CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError> {
        let key = CoreValue::string(name, budget).map_err(allocation)?;
        let binding = CoreValue::array_shared(
            [Ok(key), Ok(value), Ok(self.head.share())].into_iter(),
            budget,
        )
        .map_err(allocation)?;
        self.head = binding;
        Ok(())
    }
    pub fn get(&self, name: &str) -> Result<Option<CoreValue>, StepError> {
        let mut cursor = self.head.share();
        let mut depth = 0usize;
        loop {
            charge_work(&self.budget, depth, 1)?;
            match cursor.inspect() {
                CoreRef::Null => return Ok(None),
                CoreRef::Arr(items) if items.len() == 3 => {
                    let key = items
                        .get(0)
                        .ok_or_else(|| invalid("runtime environment binding is malformed"))?;
                    let value = items
                        .get(1)
                        .ok_or_else(|| invalid("runtime environment binding is malformed"))?;
                    let previous = items
                        .get(2)
                        .ok_or_else(|| invalid("runtime environment binding is malformed"))?;
                    if key.as_str() == Some(name) {
                        return Ok(Some(value));
                    }
                    cursor = previous;
                    depth = depth.saturating_add(1);
                }
                _ => return Err(invalid("runtime environment binding is malformed")),
            }
        }
    }
    pub fn head(&self) -> &CoreValue {
        &self.head
    }
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
        "runtime value allocation failed",
    )
}
fn invalid(message: &'static str) -> StepError {
    StepError::new("invalid", message)
}
pub fn evaluate_owned(
    ast: &CanonValue,
    env: &RuntimeEnvironment,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StepError> {
    evaluate_at(ast, env, budget, 0)
}
fn evaluate_at(
    ast: &CanonValue,
    env: &RuntimeEnvironment,
    budget: &Arc<dyn AllocationBudget>,
    depth: usize,
) -> Result<CoreValue, StepError> {
    charge_work(budget, depth, 1)?;
    match ast {
        CanonValue::Str(reference) if reference.starts_with('$') => {
            lookup(reference, env, budget, depth + 1)
        }
        CanonValue::Arr(items) => {
            let failure = RefCell::new(None);
            let result = CoreValue::array_shared(
                items.iter().map(|item| {
                    evaluate_at(item, env, budget, depth + 1).map_err(|e| {
                        failure.replace(Some(e));
                        AllocationError::Invalid
                    })
                }),
                budget,
            );
            finish(result, failure.into_inner())
        }
        CanonValue::Obj(fields) if fields.len() == 1 => {
            let (key, value) = fields.iter().next().expect("one metadata field");
            match key.as_str() {
                "eq" | "ne" => {
                    let CanonValue::Arr(args) = value else {
                        return Err(invalid("comparison needs two arguments"));
                    };
                    if args.len() != 2 {
                        return Err(invalid("comparison needs two arguments"));
                    }
                    let equal = equal(
                        &evaluate_at(&args[0], env, budget, depth + 1)?,
                        &evaluate_at(&args[1], env, budget, depth + 1)?,
                        budget,
                        depth + 1,
                    );
                    let equal = equal?;
                    CoreValue::bool(if key == "eq" { equal } else { !equal }, budget)
                        .map_err(allocation)
                }
                "not" => CoreValue::bool(
                    !boolean_owned(&evaluate_at(value, env, budget, depth + 1)?)?,
                    budget,
                )
                .map_err(allocation),
                "and" | "or" => {
                    let CanonValue::Arr(args) = value else {
                        return Err(invalid("boolean expression needs an array"));
                    };
                    let is_and = key == "and";
                    for arg in args {
                        if boolean_owned(&evaluate_at(arg, env, budget, depth + 1)?)? != is_and {
                            return CoreValue::bool(!is_and, budget).map_err(allocation);
                        }
                    }
                    CoreValue::bool(is_and, budget).map_err(allocation)
                }
                "exists" => {
                    let CanonValue::Str(reference) = value else {
                        return Err(invalid("exists needs a reference"));
                    };
                    if !reference.starts_with('$') {
                        return Err(invalid("exists needs a reference"));
                    }
                    let exists = match lookup(reference, env, budget, depth + 1) {
                        Ok(_) => true,
                        Err(error) if error.code == "not-found" => false,
                        Err(error) => return Err(error),
                    };
                    CoreValue::bool(exists, budget).map_err(allocation)
                }
                _ => object(fields, env, budget, depth + 1),
            }
        }
        CanonValue::Obj(fields) => object(fields, env, budget, depth + 1),
        _ => CoreValue::try_from_borrowed(ast, budget).map_err(allocation),
    }
}
fn finish(
    result: Result<CoreValue, AllocationError>,
    failure: Option<StepError>,
) -> Result<CoreValue, StepError> {
    match failure {
        Some(error) => Err(error),
        None => result.map_err(allocation),
    }
}
fn object(
    fields: &BTreeMap<String, CanonValue>,
    env: &RuntimeEnvironment,
    budget: &Arc<dyn AllocationBudget>,
    depth: usize,
) -> Result<CoreValue, StepError> {
    let failure = RefCell::new(None);
    let result = CoreValue::object_shared(
        fields.iter().map(|(key, value)| {
            evaluate_at(value, env, budget, depth + 1)
                .map(|value| (key.as_str(), value))
                .map_err(|e| {
                    failure.replace(Some(e));
                    AllocationError::Invalid
                })
        }),
        budget,
    );
    finish(result, failure.into_inner())
}
pub(crate) fn boolean_owned(value: &CoreValue) -> Result<bool, StepError> {
    value
        .as_bool()
        .ok_or_else(|| invalid("condition must evaluate to a boolean"))
}
fn lookup(
    reference: &str,
    env: &RuntimeEnvironment,
    budget: &Arc<dyn AllocationBudget>,
    depth: usize,
) -> Result<CoreValue, StepError> {
    let mut parts = reference.strip_prefix('$').unwrap_or(reference).split('.');
    let name = parts.next().unwrap_or_default();
    if name.is_empty() {
        return Err(invalid("empty reference"));
    }
    let missing = || StepError::new("not-found", "Flow reference is unavailable");
    let mut value = env.get(name)?.ok_or_else(missing)?;
    for part in parts {
        charge_work(budget, depth, 1)?;
        value = match value.inspect() {
            CoreRef::Obj(fields) => {
                charge_work(budget, depth, fields.len().max(1))?;
                value.get_key(part)
            }
            CoreRef::Arr(_) => part
                .parse::<usize>()
                .ok()
                .and_then(|index| value.get_index(index)),
            _ => None,
        }
        .ok_or_else(missing)?;
    }
    Ok(value)
}
fn equal(
    left: &CoreValue,
    right: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
    depth: usize,
) -> Result<bool, StepError> {
    charge_work(budget, depth, 1)?;
    match (left.inspect(), right.inspect()) {
        (CoreRef::Null, CoreRef::Null) => Ok(true),
        (CoreRef::Bool(a), CoreRef::Bool(b)) => Ok(a == b),
        (CoreRef::Int(a), CoreRef::Int(b)) => Ok(a == b),
        (CoreRef::Float(a), CoreRef::Float(b)) => Ok(a == b),
        (CoreRef::Str(a), CoreRef::Str(b)) | (CoreRef::BigInt(a), CoreRef::BigInt(b)) => {
            charge_work(budget, depth, a.len().max(b.len()).max(1))?;
            Ok(a == b)
        }
        (CoreRef::Arr(a), CoreRef::Arr(b)) => {
            if a.len() != b.len() {
                return Ok(false);
            }
            for (left, right) in a.iter().zip(b.iter()) {
                if !equal(&left, &right, budget, depth + 1)? {
                    return Ok(false);
                }
            }
            Ok(true)
        }
        (CoreRef::Obj(a), CoreRef::Obj(b)) => {
            if a.len() != b.len() {
                return Ok(false);
            }
            for ((ak, av), (bk, bv)) in a.iter().zip(b.iter()) {
                charge_work(budget, depth, ak.len().max(bk.len()).max(1))?;
                if ak != bk || !equal(&av, &bv, budget, depth + 1)? {
                    return Ok(false);
                }
            }
            Ok(true)
        }
        _ => Ok(false),
    }
}

pub(crate) fn charge_work(
    budget: &Arc<dyn AllocationBudget>,
    depth: usize,
    work: usize,
) -> Result<(), StepError> {
    if depth > MAX_DEPTH {
        return Err(allocation(AllocationError::Depth));
    }
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: u32::try_from(depth).map_err(|_| allocation(AllocationError::Overflow))?,
            work: u64::try_from(work).map_err(|_| allocation(AllocationError::Overflow))?,
        })
        .map_err(allocation)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    struct Budget {
        used: AtomicU64,
        max: u64,
    }
    impl AllocationBudget for Budget {
        fn reserve(
            &self,
            request: tobkiri_canon::AllocationRequest,
        ) -> Result<(), AllocationError> {
            self.used
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                    used.checked_add(request.bytes)
                        .filter(|total| *total <= self.max)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            self.used.fetch_sub(bytes, Ordering::SeqCst);
        }
    }
    #[test]
    fn reference_selection_shares_the_original_owner_after_environment_drop() {
        let measured = Arc::new(Budget {
            used: AtomicU64::new(0),
            max: 65536,
        });
        let budget: Arc<dyn AllocationBudget> = measured.clone();
        let value = CoreValue::try_from_borrowed(
            &CanonValue::Obj(BTreeMap::from([(
                "text".into(),
                CanonValue::Str("retained".into()),
            )])),
            &budget,
        )
        .unwrap();
        let env = RuntimeEnvironment::with_input(value, &budget).unwrap();
        let original = measured.used.load(Ordering::SeqCst);
        let detached =
            evaluate_owned(&CanonValue::Str("$input.text".into()), &env, &budget).unwrap();
        assert_eq!(measured.used.load(Ordering::SeqCst), original);
        drop(env);
        assert_eq!(detached.as_str(), Some("retained"));
        let retained = measured.used.load(Ordering::SeqCst);
        assert!(retained > 0);
        assert!(retained < original);
        assert_eq!(measured.used.load(Ordering::SeqCst), retained);
        drop(detached);
        assert_eq!(measured.used.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn failed_template_allocation_releases_partial_copy_and_preserves_input() {
        let measured = Arc::new(Budget {
            used: AtomicU64::new(0),
            max: 65536,
        });
        let budget: Arc<dyn AllocationBudget> = measured.clone();
        let input = CoreValue::string("retained", &budget).unwrap();
        let env = RuntimeEnvironment::with_input(input, &budget).unwrap();
        let original = measured.used.load(Ordering::SeqCst);
        let denied: Arc<dyn AllocationBudget> = Arc::new(Budget {
            used: AtomicU64::new(0),
            max: 1,
        });
        let template = CanonValue::Obj(BTreeMap::from([(
            "copy".into(),
            CanonValue::Str("$input".into()),
        )]));
        assert_eq!(
            evaluate_owned(&template, &env, &denied).unwrap_err().code,
            "limit-exceeded"
        );
        assert_eq!(measured.used.load(Ordering::SeqCst), original);
        assert_eq!(
            env.get("input").unwrap().unwrap().as_str(),
            Some("retained")
        );
    }
    #[test]
    fn owned_boolean_short_circuit_keeps_missing_references_unreached() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(Budget {
            used: AtomicU64::new(0),
            max: 65536,
        });
        let ast = CanonValue::Obj(BTreeMap::from([(
            "and".into(),
            CanonValue::Arr(vec![
                CanonValue::Bool(false),
                CanonValue::Str("$missing".into()),
            ]),
        )]));
        assert_eq!(
            evaluate_owned(&ast, &RuntimeEnvironment::empty(&budget).unwrap(), &budget)
                .unwrap()
                .as_bool(),
            Some(false)
        );
    }
}
