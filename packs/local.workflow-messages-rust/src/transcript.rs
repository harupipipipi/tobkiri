//! Ordered assistant tool calls, results, and their stored log projection.

use std::collections::{BTreeSet, VecDeque};

use super::{
    Error, Json, Result, array, check_depth, encode, has_fields, identifier, object, optional_null,
    parse, string, text_content,
};

const MAX_TOOL_CALLS: usize = 8;
const MAX_TRACE_BYTES: usize = 40 * 1024;
const MAX_TRACE_DEPTH: usize = 12;
const MAX_ARGUMENT_BYTES: usize = 16 * 1024;
const INVALID_TRANSCRIPT: Error = Error::Invalid("saved bridge owned tool transcript is invalid");

pub(crate) fn validated_trace<'a>(
    value: Option<&'a Json>,
    actual_logs: Option<&Json>,
    parent_role: &str,
) -> Result<&'a [Json]> {
    let Some(value) = value else {
        compare_logs(actual_logs, Vec::new())?;
        return Ok(&[]);
    };
    let trace = array(value)
        .filter(|items| items.len() <= 2 * MAX_TOOL_CALLS)
        .ok_or(Error::Invalid("saved tool transcript exceeds its bound"))?;
    if encode(value)?.len() > MAX_TRACE_BYTES {
        return Err(Error::Capacity("json input exceeds size limit"));
    }
    check_depth(value, MAX_TRACE_DEPTH)?;

    let mut pending = VecDeque::new();
    let mut seen = BTreeSet::new();
    let mut logs = Vec::new();
    for message in trace {
        if message.as_object().is_none() {
            return Err(Error::Invalid("saved tool transcript message is invalid"));
        }
        match string(message, "role") {
            Some("assistant") => {
                if !pending.is_empty() || !has_fields(message, &["role", "content", "tool_calls"]) {
                    return Err(Error::Invalid("saved tool assistant sequence is invalid"));
                }
                if !message
                    .get("content")
                    .is_some_and(|value| text_content(value, true))
                {
                    return Err(Error::Invalid("saved tool assistant content is invalid"));
                }
                let calls = message
                    .get("tool_calls")
                    .and_then(array)
                    .filter(|calls| !calls.is_empty())
                    .ok_or(Error::Invalid("saved assistant tool calls are invalid"))?;
                for item in calls {
                    pending.push_back(parse_call(item, &mut seen)?);
                }
            }
            Some("tool") if has_fields(message, &["role", "tool_call_id", "content"]) => {
                const INVALID: Error = Error::Invalid("saved tool result sequence is invalid");
                let Call {
                    id,
                    name,
                    arguments,
                } = pending.pop_front().ok_or(INVALID)?;
                let content = string(message, "content")
                    .filter(|_| string(message, "tool_call_id") == Some(id))
                    .ok_or(INVALID)?;
                logs.push(object([
                    ("tool_name", name.into()),
                    ("tool_call_id", id.into()),
                    ("arguments", arguments),
                    ("result", content.into()),
                ]));
            }
            _ => return Err(Error::Invalid("saved tool result sequence is invalid")),
        }
    }
    if !pending.is_empty() {
        return Err(Error::Invalid("saved tool transcript is incomplete"));
    }
    if !trace.is_empty() && parent_role != "assistant" {
        return Err(INVALID_TRANSCRIPT);
    }
    compare_logs(actual_logs, logs)?;
    Ok(trace)
}

struct Call<'a> {
    id: &'a str,
    name: &'a str,
    arguments: Json,
}

fn parse_call<'a>(value: &'a Json, seen: &mut BTreeSet<&'a str>) -> Result<Call<'a>> {
    const INVALID: Error = Error::Invalid("saved assistant tool call is invalid");
    if !has_fields(value, &["id", "type", "function"])
        || string(value, "type") != Some("function")
        || seen.len() >= MAX_TOOL_CALLS
    {
        return Err(INVALID);
    }
    let id = string(value, "id")
        .filter(|id| identifier(id, false, b"._:/-") && !seen.contains(id))
        .ok_or(INVALID)?;
    let function = value.get("function").ok_or(INVALID)?;
    if !has_fields(function, &["name", "arguments"]) {
        return Err(INVALID);
    }
    let name = string(function, "name")
        .filter(|name| identifier(name, false, b"._:/-"))
        .ok_or(INVALID)?;
    let arguments = string(function, "arguments").ok_or(INVALID)?;
    if arguments.len() > MAX_ARGUMENT_BYTES {
        return Err(Error::Capacity("json input exceeds size limit"));
    }
    let arguments = parse(arguments)?;
    if arguments.as_object().is_none() {
        return Err(Error::Invalid("saved tool arguments are invalid"));
    }
    seen.insert(id);
    Ok(Call {
        id,
        name,
        arguments,
    })
}

fn compare_logs(actual: Option<&Json>, expected: Vec<Json>) -> Result<()> {
    // Strict canonical values already preserve the distinctions made by byte
    // comparison (for example, true and 1) while ignoring object-key order.
    let matches = if optional_null(actual) {
        expected.is_empty()
    } else {
        actual.and_then(array) == Some(expected.as_slice())
    };
    if matches {
        Ok(())
    } else {
        Err(INVALID_TRANSCRIPT)
    }
}
