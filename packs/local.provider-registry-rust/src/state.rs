//! Pure decoded-document ports for the original Registry state machine.
//!
//! `None` means the source store is absent, not an initialized empty document.
//! Raw UTF-8/JSON decoding, Python JSONDecodeError diagnostics, filesystem
//! side effects and indented file-byte encoding belong to a future adapter.
//! These ports neither acquire a lock nor make an atomic-storage claim.
use crate::failure::{SourceError, SourceResult};
use ordered_python_compat::{
    CoercionError, MAX_INTEGER_DIGITS, PyValue, pyfloat, pyint, pyrepr, pystrip,
};

pub const STORE_VERSION: &str = "rumi.provider-registry.store.v1";
pub(crate) static NONE: PyValue = PyValue::Null;

pub trait DocumentStore {
    /// Return a detached decoded document, preserving every raw field/order.
    /// Adapter failures retain their original structured exception arguments.
    fn read(&mut self) -> SourceResult<Option<PyValue>>;
    fn write(&mut self, document: &PyValue) -> SourceResult<()>;
}

pub trait Clock {
    /// The original `_now` is evaluated only at its actual mutation stage.
    fn now(&mut self) -> SourceResult<String>;
}
impl<F: FnMut() -> SourceResult<String>> Clock for F {
    fn now(&mut self) -> SourceResult<String> {
        self()
    }
}

/// Sequential, in-memory decoded document adapter. A failed validation never
/// writes, and successful preflight never creates an absent source store.
#[derive(Debug, Default)]
pub struct MemoryStore {
    document: Option<PyValue>,
    write_count: usize,
}
impl MemoryStore {
    pub fn absent() -> Self {
        Self::default()
    }
    pub fn initialized(document: PyValue) -> Self {
        Self {
            document: Some(document),
            write_count: 0,
        }
    }
    pub fn document(&self) -> Option<&PyValue> {
        self.document.as_ref()
    }
    pub fn write_count(&self) -> usize {
        self.write_count
    }
}
impl DocumentStore for MemoryStore {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        Ok(self.document.clone())
    }
    fn write(&mut self, document: &PyValue) -> SourceResult<()> {
        self.document = Some(document.clone());
        self.write_count += 1;
        Ok(())
    }
}

pub(crate) fn object<const N: usize>(entries: [(&str, PyValue); N]) -> PyValue {
    PyValue::object(entries.into_iter().map(|(key, value)| (key.into(), value)))
}
pub(crate) fn text(value: impl Into<String>) -> PyValue {
    PyValue::String(value.into())
}
pub(crate) fn get<'a>(value: &'a PyValue, key: &str) -> &'a PyValue {
    value.get(key).unwrap_or(&NONE)
}
pub(crate) fn required<'a>(value: &'a PyValue, key: &str) -> SourceResult<&'a PyValue> {
    value.get(key).ok_or_else(|| SourceError::key_error(key))
}
pub(crate) fn put(value: &mut PyValue, key: &str, replacement: PyValue) {
    let PyValue::Object(entries) = value else {
        unreachable!("validated Python dict")
    };
    if let Some((_, current)) = entries.iter_mut().find(|(name, _)| name == key) {
        *current = replacement;
    } else {
        entries.push((key.into(), replacement));
    }
}
pub(crate) fn remove(value: &mut PyValue, key: &str) {
    let PyValue::Object(entries) = value else {
        unreachable!("validated Python dict")
    };
    let index = entries
        .iter()
        .position(|(name, _)| name == key)
        .expect("existing Python dict key");
    entries.remove(index);
}
pub(crate) fn type_name(value: &PyValue) -> &'static str {
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

fn numeric_space(character: char) -> bool {
    if character.is_ascii() {
        matches!(
            character,
            ' ' | '\t' | '\n' | '\r' | '\u{000b}' | '\u{000c}'
        )
    } else {
        let mut buffer = [0; 4];
        pystrip(character.encode_utf8(&mut buffer)).is_empty()
    }
}
/// Validate the original decimal-string grammar separately only when pyint
/// failed, so its pinned 4300-digit limit gets Python's diagnostic, not the
/// generic malformed-literal diagnostic. Individual Unicode decimal digits
/// are classified by the same pinned compatibility implementation.
fn decimal_digit_count(value: &str) -> Option<usize> {
    let value = value.trim_matches(numeric_space);
    let value = value
        .strip_prefix('+')
        .or_else(|| value.strip_prefix('-'))
        .unwrap_or(value);
    let mut digits = 0;
    let mut previous_digit = false;
    for character in value.chars() {
        if character == '_' {
            if !previous_digit {
                return None;
            }
            previous_digit = false;
        } else {
            let digit = pyint(&text(character.to_string())).ok()?;
            if digit.len() != 1 || !digit.as_bytes()[0].is_ascii_digit() {
                return None;
            }
            digits += 1;
            previous_digit = true;
        }
    }
    (digits != 0 && previous_digit).then_some(digits)
}

// CPython v3.13.9 Objects/longobject.c PyLong_FromUnicodeObject uses
// %.200R for malformed decimal strings; its digit-limit diagnostic is separate.
// https://github.com/python/cpython/blob/v3.13.9/Objects/longobject.c#L2934-L2957
fn integer_literal_repr(value: &PyValue) -> String {
    pyrepr(value).chars().take(200).collect()
}

pub(crate) fn integer(value: &PyValue) -> SourceResult<String> {
    pyint(value).map_err(|error| {
        match (error, value) {
            (CoercionError::Type, _) => SourceError::type_error(format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                type_name(value)
            )),
            (CoercionError::Value, PyValue::Float(_)) =>
                SourceError::value("cannot convert float NaN to integer"),
            (CoercionError::Overflow, PyValue::Float(_)) =>
                SourceError::new("OverflowError", "cannot convert float infinity to integer"),
            (_, PyValue::String(value)) => {
                if let Some(count) = decimal_digit_count(value).filter(|count| *count > MAX_INTEGER_DIGITS) {
                    let limit = MAX_INTEGER_DIGITS;
                    SourceError::value(format!(
                        "Exceeds the limit ({limit} digits) for integer string conversion: value has {count} digits; use sys.set_int_max_str_digits() to increase the limit"
                    ))
                } else {
                    SourceError::value(format!(
                        "invalid literal for int() with base 10: {}",
                        integer_literal_repr(&text(value))
                    ))
                }
            }
            _ => SourceError::value(format!(
                "invalid literal for int() with base 10: {}", pyrepr(value)
            )),
        }
    })
}

pub(crate) fn float(value: &PyValue) -> SourceResult<f64> {
    pyfloat(value).map_err(|error| match error {
        CoercionError::Type => SourceError::type_error(format!(
            "float() argument must be a string or a real number, not '{}'",
            type_name(value)
        )),
        CoercionError::Value => SourceError::value(format!(
            "could not convert string to float: {}",
            pyrepr(value)
        )),
        CoercionError::Overflow => {
            SourceError::new("OverflowError", "int too large to convert to float")
        }
    })
}
/// Python's exact arbitrary-precision integer addition, without converting to
/// binary64 or imposing a machine-integer revision ceiling.
pub(crate) fn increment_digits(value: &str) -> String {
    if let Some(unsigned) = value.strip_prefix('-') {
        let mut digits = unsigned.as_bytes().to_vec();
        for byte in digits.iter_mut().rev() {
            if *byte == b'0' {
                *byte = b'9';
            } else {
                *byte -= 1;
                break;
            }
        }
        let first = digits.iter().position(|byte| *byte != b'0');
        return first.map_or_else(
            || "0".into(),
            |first| {
                format!(
                    "-{}",
                    std::str::from_utf8(&digits[first..]).expect("decimal digits")
                )
            },
        );
    }
    let mut digits = value.as_bytes().to_vec();
    let mut carry = true;
    for byte in digits.iter_mut().rev() {
        if *byte == b'9' {
            *byte = b'0';
        } else {
            *byte += 1;
            carry = false;
            break;
        }
    }
    if carry {
        digits.insert(0, b'1');
    }
    String::from_utf8(digits).expect("decimal digits")
}
pub(crate) fn increment_record_revision(value: &PyValue) -> SourceResult<PyValue> {
    Ok(PyValue::Int(increment_digits(&integer(value)?)))
}
pub(crate) fn increment_store_revision(value: &PyValue) -> SourceResult<PyValue> {
    match value {
        PyValue::Int(value) => Ok(PyValue::Int(increment_digits(value))),
        PyValue::Bool(value) => Ok(PyValue::Int(if *value { "2" } else { "1" }.into())),
        PyValue::Float(value) => Ok(PyValue::Float(value + 1.0)),
        PyValue::String(_) => Err(SourceError::type_error(
            "can only concatenate str (not \"int\") to str",
        )),
        PyValue::Array(_) => Err(SourceError::type_error("'int' object is not iterable")),
        _ => Err(SourceError::type_error(format!(
            "unsupported operand type(s) for +=: '{}' and 'int'",
            type_name(value)
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn integer_addition_keeps_exact_decimal_digits() {
        for (value, expected) in [
            ("0", "1"),
            ("9", "10"),
            ("-1", "0"),
            ("-10", "-9"),
            (
                "999999999999999999999999999999",
                "1000000000000000000000000000000",
            ),
        ] {
            assert_eq!(increment_digits(value), expected);
        }
    }
    #[test]
    fn source_numeric_failure_arguments_remain_structured() {
        let bad = text("not-a-number");
        assert_eq!(
            float(&bad).unwrap_err().message,
            "could not convert string to float: 'not-a-number'"
        );
        let too_many = text("0".repeat(4301));
        let failure = integer(&too_many).unwrap_err();
        assert_eq!(failure.exception_class, "ValueError");
        assert!(failure.message.contains("value has 4301 digits"));
        assert_eq!(failure.args.len(), 1);
        let invalid = text("x".repeat(201));
        assert_eq!(
            integer(&invalid).unwrap_err().message,
            format!(
                "invalid literal for int() with base 10: '{}",
                "x".repeat(199)
            )
        );
    }
}
