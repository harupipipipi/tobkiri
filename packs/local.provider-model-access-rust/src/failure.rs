use ordered_python_compat::PyValue;

#[derive(Debug, PartialEq, Eq)]
pub struct SourceFailure {
    pub exception_class: &'static str,
    pub message: String,
}
pub type SourceResult<T> = Result<T, SourceFailure>;

impl SourceFailure {
    pub fn value(message: impl Into<String>) -> Self {
        Self {
            exception_class: "ValueError",
            message: message.into(),
        }
    }
    pub fn type_error(message: impl Into<String>) -> Self {
        Self {
            exception_class: "TypeError",
            message: message.into(),
        }
    }
    pub fn permission(message: impl Into<String>) -> Self {
        Self {
            exception_class: "PermissionError",
            message: message.into(),
        }
    }
}

pub fn type_name(value: &PyValue) -> &'static str {
    match value {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::String(_) => "str",
        PyValue::Array(_) => "list",
        PyValue::Object(_) => "dict",
    }
}

pub fn mapping(value: &PyValue) -> SourceResult<&[(String, PyValue)]> {
    value.as_object().ok_or_else(|| SourceFailure {
        exception_class: "AttributeError",
        message: format!("'{}' object has no attribute 'get'", type_name(value)),
    })
}

pub static NONE: PyValue = PyValue::Null;

pub fn get<'a>(value: &'a PyValue, key: &str) -> SourceResult<&'a PyValue> {
    Ok(mapping(value)?
        .iter()
        .find(|(name, _)| name == key)
        .map_or(&NONE, |(_, value)| value))
}

pub fn hashable(value: &PyValue) -> SourceResult<()> {
    if matches!(value, PyValue::Array(_) | PyValue::Object(_)) {
        Err(SourceFailure::type_error(format!(
            "unhashable type: '{}'",
            type_name(value)
        )))
    } else {
        Ok(())
    }
}

pub fn contains(value: &PyValue, key: &str) -> SourceResult<bool> {
    match value {
        PyValue::Object(entries) => Ok(entries.iter().any(|(name, _)| name == key)),
        PyValue::Array(values) => Ok(values.iter().any(|value| value.as_str() == Some(key))),
        PyValue::String(value) => Ok(value.contains(key)),
        _ => Err(SourceFailure::type_error(format!(
            "argument of type '{}' is not iterable",
            type_name(value)
        ))),
    }
}

pub fn subscript<'a>(value: &'a PyValue, key: &str) -> SourceResult<&'a PyValue> {
    match value {
        PyValue::Object(_) => get(value, key),
        PyValue::Array(_) => Err(SourceFailure::type_error(
            "list indices must be integers or slices, not str",
        )),
        PyValue::String(_) => Err(SourceFailure::type_error(
            "string indices must be integers, not 'str'",
        )),
        _ => Err(SourceFailure::type_error(format!(
            "'{}' object is not subscriptable",
            type_name(value)
        ))),
    }
}

pub fn unexpected_keyword(function: &str, key: &str, names: &[&str]) -> SourceFailure {
    // CPython 3.13.9 ceval.c initialize_locals uses raw %S inside fixed quotes,
    // not repr(), and considers every declared argument for a suggestion.
    let suffix = keyword_suggestion(key, names)
        .map_or_else(String::new, |name| format!(". Did you mean '{name}'?"));
    SourceFailure::type_error(format!(
        "{function}() got an unexpected keyword argument '{key}'{suffix}"
    ))
}

fn keyword_suggestion<'a>(key: &str, names: &[&'a str]) -> Option<&'a str> {
    if names.len() >= 750 {
        return None;
    }
    let mut best = usize::MAX;
    let mut suggestion = None;
    for name in names {
        if *name == key {
            continue;
        }
        let maximum = ((key.len() + name.len() + 3) * 2 / 6).min(best - 1);
        if let Some(distance) = edit_distance(key.as_bytes(), name.as_bytes(), maximum) {
            best = distance;
            suggestion = Some(*name);
        }
    }
    suggestion
}

// Python/suggestions.c measures UTF-8 bytes, trims matching affixes before
// its 40-byte bound, and gives ASCII case changes cost 1 rather than 2.
fn edit_distance(mut left: &[u8], mut right: &[u8], maximum: usize) -> Option<usize> {
    while !left.is_empty() && !right.is_empty() && left[0] == right[0] {
        left = &left[1..];
        right = &right[1..];
    }
    while !left.is_empty() && !right.is_empty() && left.last() == right.last() {
        left = &left[..left.len() - 1];
        right = &right[..right.len() - 1];
    }
    if left.is_empty() || right.is_empty() {
        let cost = (left.len() + right.len()) * 2;
        return (cost <= maximum).then_some(cost);
    }
    if left.len() > 40 || right.len() > 40 || left.len().abs_diff(right.len()) * 2 > maximum {
        return None;
    }
    let mut previous: Vec<usize> = (0..=right.len()).map(|index| index * 2).collect();
    for (index, a) in left.iter().enumerate() {
        let mut current = vec![(index + 1) * 2];
        for (column, b) in right.iter().enumerate() {
            let substitute = if a == b {
                0
            } else if a.eq_ignore_ascii_case(b) {
                1
            } else {
                2
            };
            current.push(
                (previous[column] + substitute)
                    .min(previous[column + 1] + 2)
                    .min(current[column] + 2),
            );
        }
        if *current.iter().skip(1).min().unwrap() > maximum {
            return None;
        }
        previous = current;
    }
    let distance = *previous.last().unwrap();
    (distance <= maximum).then_some(distance)
}

pub fn missing_arguments(function: &str, names: &[&str], keyword_only: bool) -> SourceFailure {
    let quoted: Vec<_> = names.iter().map(|name| format!("'{name}'")).collect();
    let list = match quoted.as_slice() {
        [one] => one.clone(),
        [one, two] => format!("{one} and {two}"),
        _ => format!(
            "{}, and {}",
            quoted[..quoted.len() - 1].join(", "),
            quoted.last().unwrap()
        ),
    };
    SourceFailure::type_error(format!(
        "{function}() missing {} required {} argument{}: {list}",
        names.len(),
        if keyword_only {
            "keyword-only"
        } else {
            "positional"
        },
        if names.len() == 1 { "" } else { "s" }
    ))
}
