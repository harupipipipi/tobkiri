use crate::{PyValue, codec::float_repr, unicode};
use std::{error::Error, fmt, fmt::Write as _};

/// Preserve the exception classes caught differently by the original adapters.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoercionError {
    Type,
    Value,
    Overflow,
}
impl fmt::Display for CoercionError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        output.write_str(match self {
            Self::Type => "Python scalar conversion requires a compatible type",
            Self::Value => "invalid value for Python scalar conversion",
            Self::Overflow => "Python numeric conversion overflow",
        })
    }
}
impl Error for CoercionError {}

pub fn pytruthy(value: &PyValue) -> bool {
    match value {
        PyValue::Null => false,
        PyValue::Bool(value) => *value,
        PyValue::Int(value) => value
            .bytes()
            .any(|byte| byte.is_ascii_digit() && byte != b'0'),
        PyValue::Float(value) => *value != 0.0,
        PyValue::String(value) => !value.is_empty(),
        PyValue::Array(value) => !value.is_empty(),
        PyValue::Object(value) => !value.is_empty(),
    }
}

/// CPython 3.13's Unicode 15.1 whitespace predicate, including U+001C..U+001F.
pub fn pystrip(value: &str) -> &str {
    value.trim_matches(unicode::is_space)
}

fn repr_string(value: &str, output: &mut String) {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    output.push(quote);
    for character in value.chars() {
        match character {
            '\\' => output.push_str("\\\\"),
            '\t' => output.push_str("\\t"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            character if character == quote => {
                output.push('\\');
                output.push(character);
            }
            character if !unicode::is_printable(character) => {
                let scalar = character as u32;
                if scalar <= 0xff {
                    write!(output, "\\x{scalar:02x}").expect("writing into String");
                } else if scalar <= 0xffff {
                    write!(output, "\\u{scalar:04x}").expect("writing into String");
                } else {
                    write!(output, "\\U{scalar:08x}").expect("writing into String");
                }
            }
            character => output.push(character),
        }
    }
    output.push(quote);
}

enum ReprPart<'a> {
    Value(&'a PyValue),
    Key(&'a str),
    Text(&'static str),
}

/// Native JSON-domain Python repr. Strings use the pinned CPython Unicode table
/// rather than the Rust toolchain's changing Unicode classification.
pub fn pyrepr(value: &PyValue) -> String {
    let mut output = String::new();
    let mut parts = vec![ReprPart::Value(value)];
    while let Some(part) = parts.pop() {
        match part {
            ReprPart::Key(value) => repr_string(value, &mut output),
            ReprPart::Text(value) => output.push_str(value),
            ReprPart::Value(value) => match value {
                PyValue::Null => output.push_str("None"),
                PyValue::Bool(value) => output.push_str(if *value { "True" } else { "False" }),
                PyValue::Int(value) => output.push_str(value),
                PyValue::Float(value) => output.push_str(&float_repr(*value)),
                PyValue::String(value) => repr_string(value, &mut output),
                PyValue::Array(values) => {
                    output.push('[');
                    parts.push(ReprPart::Text("]"));
                    for (index, value) in values.iter().enumerate().rev() {
                        parts.push(ReprPart::Value(value));
                        if index != 0 {
                            parts.push(ReprPart::Text(", "));
                        }
                    }
                }
                PyValue::Object(entries) => {
                    output.push('{');
                    parts.push(ReprPart::Text("}"));
                    for (index, (key, value)) in entries.iter().enumerate().rev() {
                        parts.push(ReprPart::Value(value));
                        parts.push(ReprPart::Text(": "));
                        parts.push(ReprPart::Key(key));
                        if index != 0 {
                            parts.push(ReprPart::Text(", "));
                        }
                    }
                }
            },
        }
    }
    output
}

pub fn pystr(value: &PyValue) -> String {
    if let PyValue::String(value) = value {
        value.clone()
    } else {
        pyrepr(value)
    }
}

fn ascii_space(character: char) -> bool {
    matches!(
        character,
        ' ' | '\t' | '\n' | '\r' | '\u{000b}' | '\u{000c}'
    )
}

/// Match _PyUnicode_TransformDecimalAndSpaceToASCII: non-ASCII decimal digits
/// and whitespace become ASCII, while ASCII controls retain their identity.
fn numeric_text(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_ascii() {
                character
            } else if let Some(decimal) = unicode::decimal(character) {
                char::from(b'0' + decimal)
            } else if unicode::is_space(character) {
                ' '
            } else {
                character
            }
        })
        .collect()
}

fn without_underscores(value: &str) -> Result<String, CoercionError> {
    let bytes = value.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'_'
            && (index == 0
                || index + 1 == bytes.len()
                || !bytes[index - 1].is_ascii_digit()
                || !bytes[index + 1].is_ascii_digit())
        {
            return Err(CoercionError::Value);
        }
    }
    Ok(value
        .chars()
        .filter(|character| *character != '_')
        .collect())
}

fn string_float(value: &str) -> Result<f64, CoercionError> {
    let transformed = numeric_text(value);
    let token = without_underscores(transformed.trim_matches(ascii_space))?;
    let (negative, unsigned) = token
        .strip_prefix('-')
        .map(|value| (true, value))
        .or_else(|| token.strip_prefix('+').map(|value| (false, value)))
        .unwrap_or((false, &token));
    if unsigned.eq_ignore_ascii_case("nan") {
        return Ok(if negative { -f64::NAN } else { f64::NAN });
    }
    if unsigned.eq_ignore_ascii_case("inf") || unsigned.eq_ignore_ascii_case("infinity") {
        return Ok(if negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    }
    // Validate Python's decimal float grammar before Rust's numeric primitive.
    let bytes = unsigned.as_bytes();
    let mut position = 0;
    while bytes.get(position).is_some_and(u8::is_ascii_digit) {
        position += 1;
    }
    let mut digits = position;
    if bytes.get(position) == Some(&b'.') {
        position += 1;
        let start = position;
        while bytes.get(position).is_some_and(u8::is_ascii_digit) {
            position += 1;
        }
        digits += position - start;
    }
    if digits == 0 {
        return Err(CoercionError::Value);
    }
    if bytes
        .get(position)
        .is_some_and(|byte| matches!(byte, b'e' | b'E'))
    {
        position += 1;
        if bytes
            .get(position)
            .is_some_and(|byte| matches!(byte, b'+' | b'-'))
        {
            position += 1;
        }
        let start = position;
        while bytes.get(position).is_some_and(u8::is_ascii_digit) {
            position += 1;
        }
        if start == position {
            return Err(CoercionError::Value);
        }
    }
    if position != bytes.len() {
        return Err(CoercionError::Value);
    }
    token.parse::<f64>().map_err(|_| CoercionError::Value)
}

/// Python float(value). A huge integer raises OverflowError, while a decimal
/// string converting to infinity successfully returns that infinity.
pub fn pyfloat(value: &PyValue) -> Result<f64, CoercionError> {
    match value {
        PyValue::Bool(value) => Ok(if *value { 1.0 } else { 0.0 }),
        PyValue::Int(value) => {
            let result = value.parse::<f64>().map_err(|_| CoercionError::Value)?;
            if result.is_finite() {
                Ok(result)
            } else {
                Err(CoercionError::Overflow)
            }
        }
        PyValue::Float(value) => Ok(*value),
        PyValue::String(value) => string_float(value),
        PyValue::Null | PyValue::Array(_) | PyValue::Object(_) => Err(CoercionError::Type),
    }
}

fn normalize_integer(value: &str) -> Result<String, CoercionError> {
    let transformed = numeric_text(value);
    let token = without_underscores(transformed.trim_matches(ascii_space))?;
    let (negative, digits) = token
        .strip_prefix('-')
        .map(|value| (true, value))
        .or_else(|| token.strip_prefix('+').map(|value| (false, value)))
        .unwrap_or((false, &token));
    if digits.is_empty()
        || !digits.bytes().all(|byte| byte.is_ascii_digit())
        || digits.len() > crate::MAX_INTEGER_DIGITS
    {
        return Err(CoercionError::Value);
    }
    let digits = digits.trim_start_matches('0');
    if digits.is_empty() {
        Ok("0".into())
    } else if negative {
        Ok(format!("-{digits}"))
    } else {
        Ok(digits.into())
    }
}

fn float_integer(value: f64) -> Result<String, CoercionError> {
    if value.is_nan() {
        return Err(CoercionError::Value);
    }
    if value.is_infinite() {
        return Err(CoercionError::Overflow);
    }
    let bits = value.to_bits();
    let exponent = ((bits >> 52) & 0x7ff) as i32 - 1023;
    if exponent < 0 {
        return Ok("0".into());
    }
    let significand = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    let mut digits = if exponent <= 52 {
        (significand >> (52 - exponent)).to_string()
    } else {
        // Base-1e9 limbs retain every bit of the binary64 integer. Formatting a
        // float's decimal approximation would lose Python int() precision.
        const BASE: u64 = 1_000_000_000;
        let mut limbs = vec![(significand % BASE) as u32, (significand / BASE) as u32];
        for _ in 0..exponent - 52 {
            let mut carry = 0u64;
            for limb in &mut limbs {
                let doubled = u64::from(*limb) * 2 + carry;
                *limb = (doubled % BASE) as u32;
                carry = doubled / BASE;
            }
            if carry != 0 {
                limbs.push(carry as u32);
            }
        }
        let mut output = limbs.pop().expect("nonzero integer has limbs").to_string();
        for limb in limbs.iter().rev() {
            write!(output, "{limb:09}").expect("writing into String");
        }
        output
    };
    if value.is_sign_negative() {
        digits.insert(0, '-');
    }
    Ok(digits)
}

/// Python int(value) with its default decimal base, returning exact digits for
/// provider_attempt even when a finite float lies beyond machine integer range.
pub fn pyint(value: &PyValue) -> Result<String, CoercionError> {
    match value {
        PyValue::Bool(value) => Ok(if *value { "1" } else { "0" }.into()),
        PyValue::Int(value) => Ok(value.clone()),
        PyValue::Float(value) => float_integer(*value),
        PyValue::String(value) => normalize_integer(value),
        PyValue::Null | PyValue::Array(_) | PyValue::Object(_) => Err(CoercionError::Type),
    }
}
