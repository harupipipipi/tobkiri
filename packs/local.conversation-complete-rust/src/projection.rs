//! The completion shape consumed by the existing Defaultspack UI and API.

use ordered_python_compat::{PyValue, pystr};

use crate::{Error, Json, Object, Result, object};

pub(crate) fn completion(mut value: Object) -> Result<Json> {
    let intents = tool_intents(value.get("tool_intents"))?;
    let mut blocks = content_blocks(value.get("output"))?;
    for intent in &intents {
        let arguments = tool_arguments(nonempty(intent.get("arguments")))?;
        blocks.push(object([
            ("type", Json::Str("tool_use".to_owned())),
            ("id", Json::Str(text_or_empty(intent.get("intent_id")))),
            ("name", Json::Str(text_or_empty(intent.get("operation")))),
            ("input", Json::Obj(arguments)),
        ]));
    }
    value.insert("content".to_owned(), Json::Arr(blocks));
    value.insert(
        "tool_calls".to_owned(),
        Json::Arr(intents.into_iter().map(Json::Obj).collect()),
    );
    Ok(Json::Obj(value))
}

fn tool_intents(value: Option<&Json>) -> Result<Vec<Object>> {
    match nonempty(value) {
        Some(Json::Arr(items)) => Ok(items
            .iter()
            .filter_map(|item| match item {
                Json::Obj(item) => Some(item.clone()),
                _ => None,
            })
            .collect()),
        // Iterating a Python dict or text yields strings, which the original
        // comprehension discards because they are not mappings.
        None | Some(Json::Obj(_) | Json::Str(_)) => Ok(Vec::new()),
        _ => Err(Error::Invalid("tool intents must be iterable")),
    }
}

fn content_blocks(output: Option<&Json>) -> Result<Vec<Json>> {
    match output {
        Some(Json::Arr(items)) => Ok(items
            .iter()
            .filter(|item| matches!(item, Json::Obj(_)))
            .cloned()
            .collect()),
        None | Some(Json::Null) => Ok(Vec::new()),
        Some(Json::Str(text)) if text.is_empty() => Ok(Vec::new()),
        // Python tests membership in {None, ""}; an object is unhashable.
        Some(Json::Obj(_)) => Err(Error::Invalid("completion output is not hashable")),
        Some(value) => Ok(vec![object([
            ("type", Json::Str("text".to_owned())),
            ("text", Json::Str(python_text(value))),
        ])]),
    }
}

fn tool_arguments(value: Option<&Json>) -> Result<Object> {
    match value {
        None => Ok(Object::new()),
        Some(Json::Obj(fields)) => Ok(fields.clone()),
        Some(Json::Arr(items)) => items.iter().map(argument_pair).collect(),
        _ => Err(invalid_arguments()),
    }
}

fn argument_pair(item: &Json) -> Result<(String, Json)> {
    match item {
        Json::Arr(pair) => match pair.as_slice() {
            [Json::Str(key), value] => Ok((key.clone(), value.clone())),
            _ => Err(invalid_arguments()),
        },
        Json::Str(pair) => {
            let mut characters = pair.chars();
            match (characters.next(), characters.next(), characters.next()) {
                (Some(key), Some(value), None) => {
                    Ok((key.to_string(), Json::Str(value.to_string())))
                }
                _ => Err(invalid_arguments()),
            }
        }
        Json::Obj(pair) if pair.len() == 2 => {
            let mut keys = pair.keys().cloned();
            match (keys.next(), keys.next()) {
                (Some(key), Some(value)) => Ok((key, Json::Str(value))),
                _ => Err(invalid_arguments()),
            }
        }
        _ => Err(invalid_arguments()),
    }
}

fn invalid_arguments() -> Error {
    Error::Invalid("tool arguments cannot be projected to a wire object")
}

fn nonempty(value: Option<&Json>) -> Option<&Json> {
    value.filter(|value| match value {
        Json::Null | Json::Bool(false) | Json::Int(0) => false,
        Json::Str(value) => !value.is_empty(),
        Json::Arr(value) => !value.is_empty(),
        Json::Obj(value) => !value.is_empty(),
        _ => true,
    })
}

fn text_or_empty(value: Option<&Json>) -> String {
    nonempty(value).map_or_else(String::new, python_text)
}

fn python_text(value: &Json) -> String {
    if let Json::Str(value) = value {
        return value.clone();
    }
    pystr(&python_value(value))
}

fn python_value(value: &Json) -> PyValue {
    match value {
        Json::Null => PyValue::Null,
        Json::Bool(value) => PyValue::Bool(*value),
        Json::Int(value) => PyValue::Int(value.to_string()),
        Json::BigInt(value) => PyValue::Int(value.clone()),
        Json::Float(value) => PyValue::Float(*value),
        Json::Str(value) => PyValue::String(value.clone()),
        Json::Arr(values) => PyValue::Array(values.iter().map(python_value).collect()),
        Json::Obj(fields) => PyValue::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), python_value(value)))
                .collect(),
        ),
    }
}
