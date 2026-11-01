//! App-owned loop state over actual Python iterators and retained work rows.
//! Control order derives from jsonschema 4.26.0 (COPYING.jsonschema).
use super::Object;
use crate::keywords::module_member;
use pyo3::{
    exceptions::PyMemoryError,
    prelude::*,
    types::{PyIterator, PyList, PyString, PyTuple},
};

pub(super) struct Pair {
    pub(super) key: Object,
    pub(super) value: Object,
}
impl Pair {
    fn unpack(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = value.py();
        let value = if (value.is_exact_instance_of::<PyTuple>()
            || value.is_exact_instance_of::<PyList>())
            && value.len()? == 2
        {
            value.clone()
        } else {
            // Keep actual UNPACK_SEQUENCE behavior for foreign pairs, including
            // __iter__, the exhaustion probe and the genuine Python exception.
            module_member(py, "app_schema_model", "unpack_keyword_pair")?.call1((value,))?
        };
        Ok(Self {
            key: value.get_item(0)?.unbind(),
            value: value.get_item(1)?.unbind(),
        })
    }
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            key: self.key.clone_ref(py),
            value: self.value.clone_ref(py),
        }
    }
}

pub(super) struct Keyword {
    pub(super) keyword: Object,
    pub(super) value: Object,
    pub(super) name: String,
}
fn select(pair: &Pair, py: Python<'_>, root: bool) -> PyResult<Option<String>> {
    let key = pair.key.bind(py);
    if key.is_exact_instance_of::<PyString>() {
        let Ok(name) = key.cast::<PyString>()?.to_str() else {
            return Ok(None); // No fixed ASCII keyword contains a surrogate.
        };
        return Ok(match name {
            "$dynamicRef"
            | "$ref"
            | "additionalProperties"
            | "allOf"
            | "anyOf"
            | "const"
            | "contains"
            | "dependentRequired"
            | "dependentSchemas"
            | "enum"
            | "exclusiveMaximum"
            | "exclusiveMinimum"
            | "format"
            | "if"
            | "items"
            | "maxItems"
            | "maxLength"
            | "maxProperties"
            | "maximum"
            | "minItems"
            | "minLength"
            | "minProperties"
            | "minimum"
            | "multipleOf"
            | "not"
            | "oneOf"
            | "pattern"
            | "patternProperties"
            | "prefixItems"
            | "properties"
            | "propertyNames"
            | "required"
            | "type"
            | "unevaluatedItems"
            | "unevaluatedProperties"
            | "uniqueItems" => Some(name.to_owned()),
            _ => None,
        });
    }
    // Unknown/non-string keys are not converted to UTF-8. A foreign key keeps
    // its actual hash/equality calls against the App's source-defined table.
    let selected = module_member(py, "app_schema_model", "select_keyword")?.call1((key, root))?;
    if selected.is_none() {
        Ok(None)
    } else {
        selected.extract().map(Some)
    }
}

pub(super) enum Keywords {
    Frozen { rows: Vec<Keyword>, next: usize },
    Live(Cursor),
}
impl Keywords {
    pub(super) fn new(schema: &Bound<'_, PyAny>, root: bool) -> PyResult<Self> {
        let py = schema.py();
        let pairs = schema.call_method0(pyo3::intern!(py, "items"))?;
        let mut cursor = if root {
            // Pinned CPython 3.13's inlined comprehension emits GET_ITER for
            // the outer expression, then GET_ITER again on that actual result
            // before FOR_ITER (compile.c:5481,5105). A foreign iterator may
            // replace itself or raise on either call. Ordinary for loops below
            // retain their single iterator acquisition.
            let outer = pairs.try_iter()?;
            Cursor::pairs(outer.as_any())?
        } else {
            Cursor::pairs(&pairs)?
        };
        if !root {
            return Ok(Self::Live(cursor));
        }
        let mut rows = Vec::new();
        // Original construction is a comprehension, not list(iterable): no
        // foreign length hint runs. Unpack/select each pair before requesting
        // the next, and retain all frozen rows until this root frame ends.
        while let Some(Entry::Pair(pair)) = cursor.next(py)? {
            if let Some(name) = select(&pair, py, true)? {
                rows.try_reserve(1)
                    .map_err(|_| PyMemoryError::new_err("native keyword work allocation"))?;
                rows.push(Keyword {
                    keyword: pair.key,
                    value: pair.value,
                    name,
                });
            }
        }
        Ok(Self::Frozen { rows, next: 0 })
    }
    pub(super) fn next(&mut self, py: Python<'_>) -> PyResult<Option<Keyword>> {
        match self {
            Self::Frozen { rows, next } => {
                let Some(row) = rows.get(*next) else {
                    return Ok(None);
                };
                *next += 1;
                Ok(Some(Keyword {
                    keyword: row.keyword.clone_ref(py),
                    value: row.value.clone_ref(py),
                    name: row.name.clone(),
                }))
            }
            Self::Live(cursor) => {
                while let Some(Entry::Pair(pair)) = cursor.next(py)? {
                    if let Some(name) = select(&pair, py, false)? {
                        return Ok(Some(Keyword {
                            keyword: pair.key,
                            value: pair.value,
                            name,
                        }));
                    }
                }
                Ok(None)
            }
        }
    }
}

struct Index {
    next: usize,
    large: Option<Object>,
}
impl Index {
    fn new() -> Self {
        Self {
            next: 0,
            large: None,
        }
    }
    fn take(&mut self, py: Python<'_>) -> PyResult<Object> {
        if let Some(current) = &self.large {
            let result = current.clone_ref(py);
            self.large = Some(current.bind(py).add(1)?.unbind());
            return Ok(result);
        }
        let result = self.next.into_pyobject(py)?.into_any().unbind();
        if let Some(next) = self.next.checked_add(1) {
            self.next = next;
        } else {
            self.large = Some(result.bind(py).add(1)?.unbind());
        }
        Ok(result)
    }
}

pub(super) enum Entry {
    Value(Object),
    Pair(Pair),
    Indexed {
        index: Object,
        value: Object,
    },
    Prefix {
        index: Object,
        instance: Object,
        schema: Object,
    },
}
impl Entry {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Value(value) => Self::Value(value.clone_ref(py)),
            Self::Pair(pair) => Self::Pair(pair.clone_ref(py)),
            Self::Indexed { index, value } => Self::Indexed {
                index: index.clone_ref(py),
                value: value.clone_ref(py),
            },
            Self::Prefix {
                index,
                instance,
                schema,
            } => Self::Prefix {
                index: index.clone_ref(py),
                instance: instance.clone_ref(py),
                schema: schema.clone_ref(py),
            },
        }
    }
}

enum Kind {
    Values(Py<PyIterator>),
    Pairs(Py<PyIterator>),
    Enumerated {
        iterator: Py<PyIterator>,
        index: Index,
    },
    Range {
        next: usize,
        end: usize,
    },
    Prefix {
        left: Box<Cursor>,
        right: Py<PyIterator>,
    },
}
pub(super) struct Cursor {
    kind: Kind,
    // Match suspended loop locals/iterator result retention across callbacks.
    // Read the next actual item before replacing the previously bound handles.
    held: Option<Entry>,
}
impl Cursor {
    fn new(kind: Kind) -> Self {
        Self { kind, held: None }
    }
    pub(super) fn values(values: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self::new(Kind::Values(values.try_iter()?.unbind())))
    }
    pub(super) fn pairs(values: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self::new(Kind::Pairs(values.try_iter()?.unbind())))
    }
    pub(super) fn enumerated(values: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self::new(Kind::Enumerated {
            iterator: values.try_iter()?.unbind(),
            index: Index::new(),
        }))
    }
    pub(super) fn range(next: usize, end: usize) -> Self {
        Self::new(Kind::Range { next, end })
    }
    pub(super) fn prefix(
        instance: &Bound<'_, PyAny>,
        schemas: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let left = Box::new(Self::enumerated(instance)?);
        let right = schemas.try_iter()?.unbind();
        Ok(Self::new(Kind::Prefix { left, right }))
    }
    pub(super) fn next(&mut self, py: Python<'_>) -> PyResult<Option<Entry>> {
        let item = match &mut self.kind {
            Kind::Values(iterator) => next(py, iterator)?.map(|value| Entry::Value(value.unbind())),
            Kind::Pairs(iterator) => next(py, iterator)?
                .map(|value| Pair::unpack(&value))
                .transpose()?
                .map(Entry::Pair),
            Kind::Enumerated { iterator, index } => match next(py, iterator)? {
                Some(value) => Some(Entry::Indexed {
                    index: index.take(py)?,
                    value: value.unbind(),
                }),
                None => None,
            },
            Kind::Range { next, end } => {
                if *next == *end {
                    None
                } else {
                    let index = (*next).into_pyobject(py)?.into_any().unbind();
                    *next += 1;
                    Some(Entry::Value(index))
                }
            }
            Kind::Prefix { left, right } => match left.next(py)? {
                Some(Entry::Indexed { index, value }) => {
                    next(py, right)?.map(|schema| Entry::Prefix {
                        index,
                        instance: value,
                        schema: schema.unbind(),
                    })
                }
                None => None,
                _ => unreachable!("prefix left is enumerated"),
            },
        };
        if let Some(item) = &item {
            self.held = Some(item.clone_ref(py));
        }
        Ok(item)
    }
}
fn next<'py>(py: Python<'py>, iterator: &Py<PyIterator>) -> PyResult<Option<Bound<'py, PyAny>>> {
    iterator.bind(py).clone().next().transpose()
}
