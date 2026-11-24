//! Actual-handle App value views. No mutable contents or decisions are cached.
//! Fixed scalar rules derive from jsonschema 4.26.0 (COPYING.jsonschema).
use super::Object;
use crate::keywords::{format, module_member, validation_error};
use pyo3::{
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    sync::PyOnceLock,
    types::{PyBool, PyDict, PyFloat, PyInt, PyIterator, PyList, PyModule, PyString, PyTuple},
};
use std::sync::atomic::{AtomicUsize, Ordering};

static FIXED_CHECKER: PyOnceLock<Object> = PyOnceLock::new();
static NATIVE_TYPES: AtomicUsize = AtomicUsize::new(0);
static PYTHON_TYPES: AtomicUsize = AtomicUsize::new(0);
static NATIVE_NUMERIC: AtomicUsize = AtomicUsize::new(0);
static PYTHON_NUMERIC: AtomicUsize = AtomicUsize::new(0);

pub(super) struct Node(Object);
impl Node {
    pub(super) fn new(value: Object) -> Self {
        Self(value)
    }
    pub(super) fn bind<'py>(&self, py: Python<'py>) -> &Bound<'py, PyAny> {
        self.0.bind(py)
    }
}

/// Fixed App policy metadata, not permission, source trust or a caller flag.
#[pyclass(module = "_tobkiri_schema_kernel")]
pub(super) struct DefaultValuePolicy {
    checker: Option<Object>,
    boundary: Option<Object>,
}
#[pymethods]
impl DefaultValuePolicy {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(value) = &self.checker {
            visit.call(value)?;
        }
        if let Some(value) = &self.boundary {
            visit.call(value)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.checker = None;
        self.boundary = None;
    }
}
#[pyfunction]
fn default_value_policy(py: Python<'_>) -> PyResult<DefaultValuePolicy> {
    let checker = FIXED_CHECKER.get_or_try_init(py, || {
        Ok::<_, PyErr>(module_member(py, "jsonschema._types", "draft202012_type_checker")?.unbind())
    })?;
    let boundary = module_member(py, "app_schema_model", "type_boundary")?;
    require_boundary(&boundary)?;
    Ok(DefaultValuePolicy {
        checker: Some(checker.clone_ref(py)),
        boundary: Some(boundary.unbind()),
    })
}

fn require_boundary(boundary: &Bound<'_, PyAny>) -> PyResult<()> {
    // PyCallable_Check inspects the type's call slot, without calling an
    // arbitrary object's metadata or executing the custom operation early.
    if !boundary.is_callable() {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "App type boundary must be callable",
        ));
    }
    Ok(())
}

pub(super) struct TypePolicy {
    checker: Object,
    fixed: Option<Object>,
    boundary: Object,
    owner: Option<Object>,
}
impl TypePolicy {
    pub(super) fn new(
        py: Python<'_>,
        checker: &Bound<'_, PyAny>,
        policy: Option<&Bound<'_, DefaultValuePolicy>>,
        owner: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let (fixed, boundary) = if let Some(policy) = policy {
            let policy = policy.try_borrow()?;
            let missing =
                || pyo3::exceptions::PyRuntimeError::new_err("App value policy was cleared");
            (
                Some(policy.checker.as_ref().ok_or_else(missing)?.clone_ref(py)),
                policy.boundary.as_ref().ok_or_else(missing)?.clone_ref(py),
            )
        } else {
            (
                None,
                module_member(py, "app_schema_model", "type_boundary")?.unbind(),
            )
        };
        // All retained handles have been cloned and the policy borrow ended.
        // Validate configuration before any Resource/Registry operation; the
        // actual owner getter and custom checker remain operation-time calls.
        require_boundary(boundary.bind(py))?;
        Ok(Self {
            checker: checker.clone().unbind(),
            fixed,
            boundary,
            owner: owner.map(|value| value.clone().unbind()),
        })
    }

    pub(super) fn current_checker<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // A selected provider's configuration may change during a callback.
        Ok(match &self.owner {
            Some(owner) => owner.bind(py).getattr(pyo3::intern!(py, "TYPE_CHECKER"))?,
            None => self.checker.bind(py).clone(),
        })
    }
    pub(super) fn check(
        &self,
        py: Python<'_>,
        value: &Bound<'_, PyAny>,
        kind: &Bound<'_, PyAny>,
        schema: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let checker = self.current_checker(py)?;
        if self
            .fixed
            .as_ref()
            .is_some_and(|fixed| checker.is(fixed.bind(py)))
            && kind.is_exact_instance_of::<PyString>()
        {
            // A surrogate-containing or otherwise unsupported type name must
            // reach the actual Python lookup/error operation, not fail while
            // merely selecting a native scalar rule.
            if let Ok(name) = kind.cast::<PyString>()?.to_str() {
                if let Some(result) = fixed_type(value, name) {
                    NATIVE_TYPES.fetch_add(1, Ordering::Relaxed);
                    return Ok(result);
                }
            }
        }
        PYTHON_TYPES.fetch_add(1, Ordering::Relaxed);
        self.boundary
            .bind(py)
            .call1((checker, value, kind, schema))?
            .is_truthy()
    }
}

fn fixed_type(value: &Bound<'_, PyAny>, kind: &str) -> Option<bool> {
    let boolean = value.is_exact_instance_of::<PyBool>();
    let integer = value.is_exact_instance_of::<PyInt>();
    let float = value.is_exact_instance_of::<PyFloat>();
    let string = value.is_exact_instance_of::<PyString>();
    let array = value.is_exact_instance_of::<PyList>();
    let object = value.is_exact_instance_of::<PyDict>();
    if !(boolean || integer || float || string || array || object || value.is_none()) {
        return None;
    }
    Some(match kind {
        "null" => value.is_none(),
        "boolean" => boolean,
        "string" => string,
        "array" => array,
        "object" => object,
        "number" => integer || float,
        "integer" => {
            integer
                || (float && {
                    let number = value.extract::<f64>().ok()?;
                    number.is_finite() && number.fract() == 0.0
                })
        }
        _ => return None,
    })
}

enum Number {
    Int(i64),
    Float(f64),
}
fn number(value: &Bound<'_, PyAny>) -> Option<Number> {
    if value.is_exact_instance_of::<PyInt>() {
        // Exact-int extraction cannot call user code. Range refusal selects
        // the actual Python comparison BEFORE doing that comparison.
        value.extract::<i64>().ok().map(Number::Int)
    } else if value.is_exact_instance_of::<PyFloat>() {
        value.extract::<f64>().ok().map(Number::Float)
    } else {
        None
    }
}
fn native_comparison(
    keyword: &str,
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
) -> Option<bool> {
    macro_rules! compare {
        ($a:expr, $b:expr) => {
            match keyword {
                "minimum" => ($a) < ($b),
                "maximum" => ($a) > ($b),
                "exclusiveMinimum" => ($a) <= ($b),
                "exclusiveMaximum" => ($a) >= ($b),
                _ => unreachable!(),
            }
        };
    }
    const EXACT: std::ops::RangeInclusive<i64> = -9_007_199_254_740_992..=9_007_199_254_740_992;
    Some(match (number(left)?, number(right)?) {
        (Number::Int(a), Number::Int(b)) => compare!(a, b),
        (Number::Float(a), Number::Float(b)) => compare!(a, b),
        (Number::Int(a), Number::Float(b)) if EXACT.contains(&a) => compare!(a as f64, b),
        (Number::Float(a), Number::Int(b)) if EXACT.contains(&b) => compare!(a, b as f64),
        _ => return None,
    })
}

pub(super) fn numeric_bound<'py>(
    py: Python<'py>,
    policy: &TypePolicy,
    keyword: &str,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !policy.check(py, instance, pyo3::intern!(py, "number").as_any(), schema)? {
        return Ok(None);
    }
    let failed = if let Some(result) = native_comparison(keyword, instance, value) {
        NATIVE_NUMERIC.fetch_add(1, Ordering::Relaxed);
        result
    } else {
        PYTHON_NUMERIC.fetch_add(1, Ordering::Relaxed);
        match keyword {
            "minimum" => instance.lt(value)?,
            "maximum" => instance.gt(value)?,
            "exclusiveMinimum" => instance.le(value)?,
            _ => instance.ge(value)?,
        }
    };
    if !failed {
        return Ok(None);
    }
    let template = match keyword {
        "minimum" => "{0!r} is less than the minimum of {1!r}",
        "maximum" => "{0!r} is greater than the maximum of {1!r}",
        "exclusiveMinimum" => "{0!r} is less than or equal to the minimum of {1!r}",
        _ => "{0!r} is greater than or equal to the maximum of {1!r}",
    };
    validation_error(
        py,
        format(py, template, &[instance.clone(), value.clone()])?,
        None,
    )
    .map(Some)
}

fn scalar(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyString>()
}

fn equal(py: Python<'_>, left: &Bound<'_, PyAny>, right: &Bound<'_, PyAny>) -> PyResult<bool> {
    if !(scalar(left) && scalar(right)) {
        return module_member(py, "app_schema_model", "equality_boundary")?
            .call1((left, right))?
            .is_truthy();
    }
    // Source equality tests identity before numeric equality: a retained NaN
    // equals itself, while two distinct NaNs do not. Bool is never a number.
    if left.is(right) {
        return Ok(true);
    }
    if left.is_none() || right.is_none() {
        return Ok(false);
    }
    if left.is_exact_instance_of::<PyString>() || right.is_exact_instance_of::<PyString>() {
        // Exact Unicode's C comparison also handles lone surrogates without
        // converting either live value into a UTF-8-only representation.
        return left.eq(right);
    }
    if left.is_exact_instance_of::<PyBool>() || right.is_exact_instance_of::<PyBool>() {
        return Ok(false);
    }
    const EXACT: std::ops::RangeInclusive<i64> = -9_007_199_254_740_992..=9_007_199_254_740_992;
    Ok(match (number(left), number(right)) {
        (Some(Number::Int(a)), Some(Number::Int(b))) => a == b,
        (Some(Number::Float(a)), Some(Number::Float(b))) => a == b,
        (Some(Number::Int(a)), Some(Number::Float(b))) if EXACT.contains(&a) => a as f64 == b,
        (Some(Number::Float(a)), Some(Number::Int(b))) if EXACT.contains(&b) => a == b as f64,
        // Arbitrary-size exact ints and unsafe int/float pairs retain Python's
        // exact primitive rich comparison; no approximate numeric conversion.
        _ => return left.eq(right),
    })
}

fn single_type(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if value.is_instance_of::<PyString>() {
        return Ok(true); // Actual str subclasses keep this exact kind object.
    }
    if value.is_none()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyList>()
        || value.is_exact_instance_of::<PyDict>()
        || value.is_exact_instance_of::<PyTuple>()
    {
        return Ok(false);
    }
    // Source isinstance(value, str) can read a foreign __class__ property.
    module_member(value.py(), "app_schema_model", "type_is_single")?
        .call1((value,))?
        .is_truthy()
}

fn genexpr_iterator<'py>(values: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyIterator>> {
    // Pinned 3.13 emits outer GET_ITER, then GET_ITER on that result when the
    // generator expression starts. Preserve both actual foreign operations.
    let outer = values.try_iter()?;
    outer.as_any().try_iter()
}

fn type_rule<'py>(
    py: Python<'py>,
    policy: &TypePolicy,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let single = single_type(value)?;
    if single {
        if policy.check(py, instance, value, schema)? {
            return Ok(None);
        }
    } else {
        let mut iterator = genexpr_iterator(value)?;
        while let Some(kind) = iterator.next() {
            if policy.check(py, instance, &kind?, schema)? {
                return Ok(None);
            }
        }
    }
    // Failure formatting re-iterates the original kinds, just as the original
    // repr generator does. No temporary singleton list or kind snapshot exists.
    module_member(py, "app_schema_model", "type_error")?
        .call1((instance, value, single))
        .map(Some)
}

fn length_rule<'py>(
    py: Python<'py>,
    policy: &TypePolicy,
    keyword: &str,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let kind = match keyword {
        "minItems" | "maxItems" => pyo3::intern!(py, "array"),
        "minLength" | "maxLength" => pyo3::intern!(py, "string"),
        _ => pyo3::intern!(py, "object"),
    };
    if !policy.check(py, instance, kind.as_any(), schema)? {
        return Ok(None);
    }
    if keyword == "maxProperties" && !policy.check(py, instance, kind.as_any(), schema)? {
        return Ok(None); // This second selected type check is in the source.
    }
    let length = instance.len()?; // Actual protocol, once per constraint visit.
    let minimum = keyword.starts_with("min");
    let failed = if value.is_exact_instance_of::<PyInt>() {
        match value.extract::<i64>() {
            Ok(bound) => {
                if minimum {
                    (length as i128) < bound as i128
                } else {
                    (length as i128) > bound as i128
                }
            }
            Err(_) => {
                let actual_length = length.into_pyobject(py)?.into_any();
                if minimum {
                    actual_length.lt(value)?
                } else {
                    actual_length.gt(value)?
                }
            }
        }
    } else {
        let actual_length = length.into_pyobject(py)?.into_any();
        if minimum {
            actual_length.lt(value)?
        } else {
            actual_length.gt(value)?
        }
    };
    if !failed {
        return Ok(None);
    }
    let adjective = if minimum {
        if value.eq(1)? {
            "should be non-empty"
        } else if keyword == "minProperties" {
            "does not have enough properties"
        } else {
            "is too short"
        }
    } else if value.eq(0)? {
        "is expected to be empty"
    } else if keyword == "maxProperties" {
        "has too many properties"
    } else {
        "is too long"
    };
    validation_error(
        py,
        format(
            py,
            "{0!r} {1}",
            &[instance.clone(), PyString::new(py, adjective).into_any()],
        )?,
        None,
    )
    .map(Some)
}

pub(super) fn scalar_constraint<'py>(
    py: Python<'py>,
    policy: &TypePolicy,
    keyword: &str,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    match keyword {
        "type" => type_rule(py, policy, value, instance, schema),
        "minimum" | "maximum" | "exclusiveMinimum" | "exclusiveMaximum" => {
            numeric_bound(py, policy, keyword, value, instance, schema)
        }
        "minItems" | "maxItems" | "minLength" | "maxLength" | "minProperties" | "maxProperties" => {
            length_rule(py, policy, keyword, value, instance, schema)
        }
        "const" | "enum" => {
            if keyword == "const" {
                if equal(py, instance, value)? {
                    return Ok(None);
                }
            } else {
                let mut iterator = genexpr_iterator(value)?;
                while let Some(each) = iterator.next() {
                    if equal(py, &each?, instance)? {
                        return Ok(None);
                    }
                }
            }
            let constructor = module_member(py, "jsonschema.exceptions", "ValidationError")?;
            let message = if keyword == "const" {
                format(py, "{0!r} was expected", &[value.clone()])?
            } else {
                format(
                    py,
                    "{0!r} is not one of {1!r}",
                    &[instance.clone(), value.clone()],
                )?
            };
            constructor.call1((message,)).map(Some)
        }
        _ => unreachable!("closed App scalar constraint"),
    }
}

#[pyfunction]
fn app_value_counts(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let result = PyDict::new(py);
    for (name, counter) in [
        ("native_type_checks", &NATIVE_TYPES),
        ("python_type_checks", &PYTHON_TYPES),
        ("native_numeric_comparisons", &NATIVE_NUMERIC),
        ("python_numeric_comparisons", &PYTHON_NUMERIC),
    ] {
        result.set_item(name, counter.load(Ordering::Relaxed))?;
    }
    Ok(result)
}
pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<DefaultValuePolicy>()?;
    module.add_function(wrap_pyfunction!(default_value_policy, module)?)?;
    module.add_function(wrap_pyfunction!(app_value_counts, module)?)?;
    Ok(())
}
