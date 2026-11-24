//! Independent object-keyword control over actual Python data protocols.
//! Derived from jsonschema 4.26.0 _keywords.py/_utils.py (COPYING.jsonschema).
use super::{
    cursor::{Cursor, Entry, Pair},
    value::TypePolicy,
    Object,
};
use crate::keywords::module_member;
use pyo3::{
    prelude::*,
    types::{PyDict, PySet, PyString},
};

pub(super) struct Child {
    pub(super) schema: Object,
    pub(super) instance: Object,
    pub(super) path: Object,
    pub(super) schema_path: Option<Object>,
}

pub(super) enum Start {
    Skip,
    Cursor(State),
    Error { error: Object, state: State },
}

pub(super) struct Pattern {
    pattern: Object,
    schema: Object,
    items: Cursor,
}

pub(super) enum State {
    Properties(Cursor),
    Patterns {
        patterns: Cursor,
        current: Option<Pattern>,
        // Source k/v locals survive an exhausted inner loop, even when the
        // next pattern has no items. Keep those same bound handles alive.
        last_item: Option<Pair>,
    },
    Additional(Cursor),
    Rejected {
        _extras: Object,
    },
}

fn search(py: Python<'_>, pattern: &Bound<'_, PyAny>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
    // This shared regex dependency is an actual operation, selected anew for
    // every key. Generic keyword/helper globals are not App algorithm hooks.
    module_member(py, "re", "search")?
        .call1((pattern, key))?
        .is_truthy()
}

fn extras<'py>(
    py: Python<'py>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PySet>> {
    let extras = PySet::empty(py)?;
    let properties = schema.call_method1("get", ("properties", PyDict::new(py)))?;
    let patterns = PyString::new(py, "|").call_method1(
        "join",
        (schema.call_method1("get", ("patternProperties", PyDict::new(py)))?,),
    )?;
    // Source's generator binds both handles before acquiring this iterator.
    // Add each yielded key to the real Python set before advancing again, so
    // foreign hash/equality calls and the set's iteration order are preserved.
    let mut keys = Cursor::values(instance)?;
    while let Some(Entry::Value(key)) = keys.next(py)? {
        let key = key.bind(py);
        if !properties.contains(key)? && !(patterns.is_truthy()? && search(py, &patterns, key)?) {
            extras.add(key)?;
        }
    }
    Ok(extras)
}

pub(super) fn start<'py>(
    py: Python<'py>,
    policy: &TypePolicy,
    keyword: &str,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Start> {
    if !policy.check(py, instance, pyo3::intern!(py, "object").as_any(), schema)? {
        return Ok(Start::Skip);
    }
    Ok(match keyword {
        "properties" => Start::Cursor(State::Properties(Cursor::pairs(
            &value.call_method0("items")?,
        )?)),
        "patternProperties" => Start::Cursor(State::Patterns {
            patterns: Cursor::pairs(&value.call_method0("items")?)?,
            current: None,
            last_item: None,
        }),
        "additionalProperties" => {
            // Classification is complete before the aP type check. A callback
            // there can mutate live inputs/schema without changing this set.
            let extras = extras(py, instance, schema)?;
            if policy.check(py, value, pyo3::intern!(py, "object").as_any(), schema)? {
                Start::Cursor(State::Additional(Cursor::values(extras.as_any())?))
            } else if !value.is_truthy()? && extras.is_truthy()? {
                let error = module_member(py, "app_schema_model", "additional_properties_error")?
                    .call1((&extras, schema))?
                    .unbind();
                // The source keyword is suspended at yield while its actual
                // extras set remains live, including during error decoration.
                Start::Error {
                    error,
                    state: State::Rejected {
                        _extras: extras.into_any().unbind(),
                    },
                }
            } else {
                Start::Skip
            }
        }
        _ => unreachable!("closed App object keyword"),
    })
}

impl State {
    pub(super) fn next(
        &mut self,
        py: Python<'_>,
        instance: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Child>> {
        match self {
            Self::Rejected { .. } => Ok(None),
            Self::Properties(properties) => {
                while let Some(Entry::Pair(Pair { key, value: schema })) = properties.next(py)? {
                    if instance.contains(key.bind(py))? {
                        return Ok(Some(Child {
                            instance: instance.get_item(key.bind(py))?.unbind(),
                            schema,
                            schema_path: Some(key.clone_ref(py)),
                            path: key,
                        }));
                    }
                }
                Ok(None)
            }
            Self::Additional(extras) => match extras.next(py)? {
                Some(Entry::Value(key)) => Ok(Some(Child {
                    instance: instance.get_item(key.bind(py))?.unbind(),
                    schema: value.clone().unbind(),
                    path: key,
                    schema_path: None,
                })),
                None => Ok(None),
                _ => unreachable!("additional property cursor"),
            },
            Self::Patterns {
                patterns,
                current,
                last_item,
            } => loop {
                if let Some(pattern) = current {
                    if let Some(Entry::Pair(pair)) = pattern.items.next(py)? {
                        *last_item = Some(Pair {
                            key: pair.key.clone_ref(py),
                            value: pair.value.clone_ref(py),
                        });
                        if search(py, pattern.pattern.bind(py), pair.key.bind(py))? {
                            return Ok(Some(Child {
                                schema: pattern.schema.clone_ref(py),
                                instance: pair.value,
                                path: pair.key,
                                schema_path: Some(pattern.pattern.clone_ref(py)),
                            }));
                        }
                        continue;
                    }
                    *current = None;
                }
                match patterns.next(py)? {
                    Some(Entry::Pair(Pair { key, value: schema })) => {
                        *current = Some(Pattern {
                            pattern: key,
                            schema,
                            items: Cursor::pairs(&instance.call_method0("items")?)?,
                        });
                    }
                    None => return Ok(None),
                    _ => unreachable!("pattern property cursor"),
                }
            },
        }
    }
}
