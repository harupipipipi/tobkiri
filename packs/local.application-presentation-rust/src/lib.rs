//! Sealed Defaultspack presentation data for the root-level PackVM ABI.

use serde_json::{Number, Value};

#[cfg(feature = "component")]
mod component;

#[cfg(not(feature = "component"))]
mod reference_component;

const OPERATION: &str = "defaultspack.presentation.read";
const UI: &str = include_str!("../data/ui.json");
const COMMANDS: &str = include_str!("../data/commands.json");
const INPUT_LIMIT: usize = 64 * 1024;
const MAX_OPTIONS: usize = 256;
const MAX_OPTION_CHARS: usize = 4096;
const MODEL_FIELDS: [&str; 4] = [
    "main_model",
    "lightweight_model",
    "preferred_model",
    "model_api_routes",
];
const INVALID_FIELDS: &str = "application presentation fields are invalid";
const INVALID_EMBEDDED_UI: &str = "application presentation UI data is invalid";
const INPUT_TOO_LARGE: &str = "application presentation input exceeds limit";

/// Read the embedded presentation and apply any supplied model display options.
pub fn invoke(operation_id: &str, payload_json: &str) -> Result<String, &'static str> {
    if operation_id != OPERATION {
        return Err("application presentation operation is invalid");
    }

    let payload: Value = serde_json::from_str(payload_json).map_err(|_| INVALID_FIELDS)?;
    let fields = payload.as_object().ok_or(INVALID_FIELDS)?;
    if fields.keys().any(|key| {
        !matches!(
            key.as_str(),
            "kind" | "model_options" | "profile_id" | "_session_id"
        )
    }) {
        return Err(INVALID_FIELDS);
    }
    if python_json_len(&payload) > INPUT_LIMIT {
        return Err(INPUT_TOO_LARGE);
    }

    let options = fields.get("model_options");
    match fields.get("kind").and_then(Value::as_str) {
        Some("ui") => render_ui(options),
        Some("commands") if options.is_none() => Ok(COMMANDS.trim_end().to_owned()),
        _ => Err("application presentation kind is invalid"),
    }
}

fn render_ui(options: Option<&Value>) -> Result<String, &'static str> {
    let Some(options) = options else {
        return Ok(UI.trim_end().to_owned());
    };
    let items = options
        .as_array()
        .filter(|items| items.len() <= MAX_OPTIONS)
        .ok_or("model display options are invalid")?;
    for item in items {
        let fields = item
            .as_object()
            .filter(|fields| {
                fields.len() == 2 && fields.contains_key("value") && fields.contains_key("label")
            })
            .ok_or("model display option fields are invalid")?;
        if fields.values().any(|value| {
            !value
                .as_str()
                .is_some_and(|text| !text.is_empty() && text.chars().count() <= MAX_OPTION_CHARS)
        }) {
            return Err("model display option value is invalid");
        }
    }
    if items.is_empty() {
        return Ok(UI.trim_end().to_owned());
    }

    let mut ui: Value = serde_json::from_str(UI).map_err(|_| INVALID_EMBEDDED_UI)?;
    let model_fields = ui["sections"]
        .as_array_mut()
        .and_then(|sections| {
            sections
                .iter_mut()
                .find(|section| section["id"] == "models")
        })
        .and_then(|section| section["fields"].as_array_mut())
        .ok_or(INVALID_EMBEDDED_UI)?;
    for id in MODEL_FIELDS {
        let target = model_fields
            .iter_mut()
            .find(|field| field["id"] == id)
            .and_then(|field| field.get_mut("options"))
            .ok_or(INVALID_EMBEDDED_UI)?;
        *target = options.clone();
    }
    serde_json::to_string(&ui).map_err(|_| INVALID_EMBEDDED_UI)
}

// Python measures parsed data using json.dumps defaults (ASCII escapes and
// spaces following separators), independently of the transport representation.
fn python_json_len(value: &Value) -> usize {
    match value {
        Value::Null | Value::Bool(true) => 4,
        Value::Bool(false) => 5,
        Value::Number(number) => python_number_len(number),
        Value::String(text) => python_string_len(text),
        Value::Array(items) => {
            2 + 2 * items.len().saturating_sub(1) + items.iter().map(python_json_len).sum::<usize>()
        }
        Value::Object(fields) => {
            2 + 2 * fields.len().saturating_sub(1)
                + fields
                    .iter()
                    .map(|(key, value)| python_string_len(key) + 2 + python_json_len(value))
                    .sum::<usize>()
        }
    }
}

fn python_string_len(text: &str) -> usize {
    2 + text
        .chars()
        .map(|character| match character {
            '"' | '\\' | '\u{8}' | '\t' | '\n' | '\u{c}' | '\r' => 2,
            ' '..='~' => 1,
            '\u{10000}'.. => 12,
            _ => 6,
        })
        .sum::<usize>()
}

fn python_number_len(number: &Number) -> usize {
    let Some(value) = number.as_f64().filter(|_| number.is_f64()) else {
        return number.to_string().len();
    };
    // Floats are outside the signed JSON profile. Use the corresponding
    // decimal/scientific notation for callers testing raw JSON directly.
    let text = format!("{value:?}");
    match text.split_once('e') {
        Some((mantissa, exponent)) => {
            let digits = exponent.trim_start_matches(['-', '+']);
            mantissa.len() + 2 + digits.len().max(2)
        }
        None => text.len(),
    }
}

#[cfg(test)]
mod tests;
