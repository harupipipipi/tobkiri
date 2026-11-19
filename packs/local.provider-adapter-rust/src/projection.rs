use ordered_python_compat::{
    CodecError, MAX_BYTES, MAX_NODES, PyValue, dumps_bounded, dumps_python_json, loads,
    loads_python_json, pystr, pytruthy,
};
use std::collections::BTreeMap;

// CPython's decoder counts nesting against the runtime's recursion budget.
// Leave deeply nested replies to that decoder, including overwritten fields.
const MAX_PROJECTION_DEPTH: usize = 256;

/// Project one finite loopback completion into the legacy local adapter result.
/// `None` leaves the original response available to the Python interpreter.
pub fn project_local_completion(raw: &str, stream: bool) -> Option<String> {
    if !supports_input_shape(raw) {
        return None;
    }
    let value = match loads(raw) {
        Ok(value) => value,
        // Python retains the last value at the first position of a repeated
        // decoded key. The ordinary ordered codec rejects those keys.
        Err(CodecError::Invalid) => {
            let ordered = loads_python_json(raw).ok()?;
            loads(&dumps_python_json(&ordered).ok()?).ok()?
        }
        Err(_) => return None,
    };
    value.as_object()?;

    let first = value
        .get("choices")
        .and_then(PyValue::as_array)
        .and_then(|choices| choices.first());
    let message = first.and_then(|first| first.get("message"));
    let output = message
        .and_then(|message| message.get("content"))
        .filter(|content| !matches!(content, PyValue::Null))
        .cloned()
        .unwrap_or_else(|| PyValue::String(String::new()));
    let tool_intents = tool_intents(message.and_then(|message| message.get("tool_calls")))?;
    let usage = usage(value.get("usage"))?;
    let finish_reason = first
        .and_then(|first| first.get("finish_reason"))
        .cloned()
        .unwrap_or(PyValue::Null);

    let events = stream.then(|| stream_events(&output, &tool_intents, &usage, &finish_reason));
    let mut fields = vec![
        ("output".into(), output),
        ("tool_intents".into(), PyValue::Array(tool_intents)),
        ("usage".into(), usage),
        ("finish_reason".into(), finish_reason),
    ];
    if let Some(events) = events {
        fields.push(("events".into(), PyValue::Array(events)));
    }
    dumps_bounded(&PyValue::Object(fields), MAX_BYTES).ok()
}

fn supports_input_shape(raw: &str) -> bool {
    if raw.len() > MAX_BYTES {
        return false;
    }
    let mut bytes = raw.bytes();
    let mut quoted = false;
    let mut depth = 0;
    let mut structure = 0;
    while let Some(byte) = bytes.next() {
        match byte {
            b'\\' if quoted => {
                bytes.next();
            }
            b'"' => quoted = !quoted,
            b'[' | b'{' if !quoted => {
                depth += 1;
                structure += 1;
            }
            b']' | b'}' if !quoted => {
                if depth == 0 {
                    return false;
                }
                depth -= 1;
            }
            b',' | b':' if !quoted => structure += 1,
            _ => {}
        }
        // Openers and separators bound the node count even when duplicate
        // keys make the Python-compatible decoder's final tree much smaller.
        if depth > MAX_PROJECTION_DEPTH || structure > MAX_NODES {
            return false;
        }
    }
    true
}

fn tool_intents(value: Option<&PyValue>) -> Option<Vec<PyValue>> {
    match value.filter(|value| pytruthy(value)) {
        None => Some(Vec::new()),
        Some(PyValue::Array(items)) => Some(items.clone()),
        Some(PyValue::Object(items)) => Some(
            items
                .iter()
                .map(|(key, _)| PyValue::String(key.clone()))
                .collect(),
        ),
        Some(PyValue::String(items)) => {
            let count = items.chars().take(MAX_NODES + 1).count();
            if count > MAX_NODES {
                return None;
            }
            Some(
                items
                    .chars()
                    .map(|item| PyValue::String(item.to_string()))
                    .collect(),
            )
        }
        Some(_) => None,
    }
}

fn usage(value: Option<&PyValue>) -> Option<PyValue> {
    match value.filter(|value| pytruthy(value)) {
        None => Some(PyValue::Object(Vec::new())),
        Some(value @ PyValue::Object(_)) => Some(value.clone()),
        Some(PyValue::Array(pairs)) => {
            let mut entries: Vec<(String, PyValue)> = Vec::new();
            let mut positions: BTreeMap<String, usize> = BTreeMap::new();
            for pair in pairs {
                let (key, value) = usage_pair(pair)?;
                if let Some(index) = positions.get(&key).copied() {
                    entries[index].1 = value;
                } else {
                    positions.insert(key.clone(), entries.len());
                    entries.push((key, value));
                }
            }
            Some(PyValue::Object(entries))
        }
        Some(_) => None,
    }
}

fn usage_pair(pair: &PyValue) -> Option<(String, PyValue)> {
    match pair {
        PyValue::Array(items) => {
            let [key, value] = items.as_slice() else {
                return None;
            };
            // JSON object syntax cannot round-trip non-string Python dict keys.
            Some((key.as_str()?.to_owned(), value.clone()))
        }
        PyValue::Object(items) => {
            let [(key, _), (value, _)] = items.as_slice() else {
                return None;
            };
            Some((key.clone(), PyValue::String(value.clone())))
        }
        PyValue::String(items) => {
            let mut chars = items.chars();
            let key = chars.next()?;
            let value = chars.next()?;
            chars
                .next()
                .is_none()
                .then(|| (key.to_string(), PyValue::String(value.to_string())))
        }
        _ => None,
    }
}

fn stream_events(
    output: &PyValue,
    tool_intents: &[PyValue],
    usage: &PyValue,
    finish_reason: &PyValue,
) -> Vec<PyValue> {
    let mut events = Vec::new();
    if pytruthy(output) {
        events.push(event("text_delta", "delta", PyValue::String(pystr(output))));
    }
    events.extend(
        tool_intents
            .iter()
            .filter(|intent| intent.as_object().is_some())
            .map(|intent| event("tool_intent_delta", "tool_intent", intent.clone())),
    );
    events.push(event("usage", "usage", usage.clone()));
    events.push(event("finish", "finish_reason", finish_reason.clone()));
    events
}

fn event(kind: &str, key: &str, value: PyValue) -> PyValue {
    PyValue::Object(vec![
        ("type".into(), PyValue::String(kind.into())),
        (key.into(), value),
    ])
}

#[cfg(test)]
mod tests;
