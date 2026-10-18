//! Exact storage@1.1 SQL cells. Integers are canonical decimal text, and text
//! remains the original UTF-8 payload inside one mandatory type/value envelope.
use crate::engine::ToolError;
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Cell {
    #[serde(rename = "type")]
    kind: String,
    value: Value,
}

pub(crate) fn text(value: &str) -> String {
    json!({"type": "text", "value": value}).to_string()
}

pub(crate) fn integer(value: i64) -> String {
    json!({"type": "integer", "value": value.to_string()}).to_string()
}

fn payload(input: &str, expected: &str) -> Result<String, ToolError> {
    let cell: Cell = serde_json::from_str(input)
        .map_err(|_| ToolError::new("backend", "invalid typed SQL cell envelope"))?;
    if cell.kind != expected {
        return Err(ToolError::new("backend", "typed SQL cell kind mismatch"));
    }
    cell.value
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| ToolError::new("backend", "typed SQL cell payload must be text"))
}

pub(crate) fn read_text(input: &str) -> Result<String, ToolError> {
    payload(input, "text")
}

pub(crate) fn read_integer(input: &str) -> Result<i64, ToolError> {
    let text = payload(input, "integer")?;
    let value = text
        .parse::<i64>()
        .map_err(|_| ToolError::new("backend", "typed SQL integer exceeds signed 64-bit range"))?;
    if value.to_string() != text {
        return Err(ToolError::new(
            "backend",
            "typed SQL integer is not canonical",
        ));
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_cells_preserve_json_unicode_and_quotes_without_double_encoding() {
        let original = "{\"answer\":391,\"text\":\"猫\\n\\\"\"}";
        let encoded = text(original);
        let cell: Value = serde_json::from_str(&encoded).unwrap();
        assert_eq!(cell["type"], "text");
        assert_eq!(cell["value"], original);
        assert_eq!(read_text(&encoded).unwrap(), original);
        assert_eq!(read_text(&text("plain text")).unwrap(), "plain text");
    }

    #[test]
    fn integer_cells_preserve_full_i64_as_decimal_text() {
        for value in [i64::MIN, -9_007_199_254_740_992, -1, 0, 1, i64::MAX] {
            let encoded = integer(value);
            let cell: Value = serde_json::from_str(&encoded).unwrap();
            assert_eq!(cell["type"], "integer");
            assert_eq!(cell["value"], value.to_string());
            assert_eq!(read_integer(&encoded).unwrap(), value);
        }
    }

    #[test]
    fn typed_cells_reject_legacy_scalars_unknown_fields_and_wrong_types() {
        for malformed in [
            r#""legacy scalar""#,
            r#"{"type":"text","value":"text","extra":true}"#,
            r#"{"type":"text","value":null}"#,
            r#"{"type":"integer","value":"1"}"#,
            r#"{"type":"text","value":"first","value":"second"}"#,
        ] {
            assert!(read_text(malformed).is_err(), "{malformed}");
        }
    }

    #[test]
    fn integer_cells_reject_numeric_json_loss_and_noncanonical_or_overflow_values() {
        assert!(read_integer(r#"{"type":"integer","value":1}"#).is_err());
        for value in [
            "-0",
            "00",
            "+1",
            "1.0",
            "1e0",
            "9223372036854775808",
            "-9223372036854775809",
        ] {
            let encoded = json!({"type": "integer", "value": value}).to_string();
            assert!(read_integer(&encoded).is_err(), "{value}");
        }
    }
}
