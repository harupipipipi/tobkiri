use tobkiri_canon::{CanonValue, MAX_DEPTH, MAX_SAFE_INTEGER};

/// The native host transport admits at most one MiB of control data. Recheck
/// the already parsed argument graph without cloning it before Core allocates
/// its budgeted RuntimeValue copy.
const MAX_UI_ARGUMENT_BYTES: usize = 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Error {
    Invalid,
    Limit,
}

pub(crate) fn validate_args(value: &CanonValue) -> Result<(), Error> {
    let mut size = 0usize;
    encoded_size(value, 0, &mut size)?;
    Ok(())
}

fn add(size: &mut usize, amount: usize) -> Result<(), Error> {
    *size = size.checked_add(amount).ok_or(Error::Limit)?;
    if *size > MAX_UI_ARGUMENT_BYTES {
        return Err(Error::Limit);
    }
    Ok(())
}

fn encoded_size(value: &CanonValue, depth: usize, size: &mut usize) -> Result<(), Error> {
    if depth > MAX_DEPTH {
        return Err(Error::Limit);
    }
    match value {
        CanonValue::Null => add(size, 4),
        CanonValue::Bool(true) => add(size, 4),
        CanonValue::Bool(false) => add(size, 5),
        CanonValue::Int(number) => {
            if number.unsigned_abs() > MAX_SAFE_INTEGER {
                return Err(Error::Invalid);
            }
            let mut magnitude = number.unsigned_abs();
            let mut digits = 1usize;
            while magnitude >= 10 {
                magnitude /= 10;
                digits += 1;
            }
            add(size, digits + if *number < 0 { 1 } else { 0 })
        }
        CanonValue::BigInt(number) => {
            let digits = number.strip_prefix('-').unwrap_or(number);
            if digits.is_empty()
                || !digits.bytes().all(|byte| byte.is_ascii_digit())
                || (digits.len() > 1 && digits.starts_with('0'))
                || digits.len() > 16
                || (digits.len() == 16 && digits > "9007199254740991")
            {
                return Err(Error::Invalid);
            }
            // StrictV1 normalizes either spelling of zero to the single byte
            // `0`, matching the canonical encoder.
            add(size, if digits == "0" { 1 } else { number.len() })
        }
        CanonValue::Float(_) => Err(Error::Invalid),
        CanonValue::Str(string) => string_size(string, size),
        CanonValue::Arr(items) => {
            add(size, 1)?;
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    add(size, 1)?;
                }
                encoded_size(item, depth + 1, size)?;
            }
            add(size, 1)
        }
        CanonValue::Obj(fields) => {
            add(size, 1)?;
            for (index, (key, item)) in fields.iter().enumerate() {
                if index > 0 {
                    add(size, 1)?;
                }
                string_size(key, size)?;
                add(size, 1)?; // colon
                encoded_size(item, depth + 1, size)?;
            }
            add(size, 1)
        }
    }
}

fn string_size(value: &str, size: &mut usize) -> Result<(), Error> {
    add(size, 1)?; // opening quote
    for character in value.chars() {
        let bytes = match character {
            '"' | '\\' | '\n' | '\r' | '\t' | '\u{8}' | '\u{c}' => 2,
            '\u{0}'..='\u{1f}' => 6,
            _ => character.len_utf8(),
        };
        add(size, bytes)?;
    }
    add(size, 1) // closing quote
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[test]
    fn counts_strict_escaping_and_rejects_legacy_scalars() {
        let value = CanonValue::Str("a\n\u{1}".into());
        assert_eq!(validate_args(&value), Ok(()));
        assert_eq!(encoded_size_for_test(&value), 11);
        assert_eq!(validate_args(&CanonValue::Float(1.0)), Err(Error::Invalid));
        assert_eq!(
            validate_args(&CanonValue::BigInt("9007199254740992".into())),
            Err(Error::Invalid)
        );
    }

    #[test]
    fn rejects_a_native_control_graph_over_one_mib() {
        let value = CanonValue::Str("x".repeat(MAX_UI_ARGUMENT_BYTES));
        assert_eq!(validate_args(&value), Err(Error::Limit));
    }

    fn encoded_size_for_test(value: &CanonValue) -> usize {
        let mut size = 0;
        encoded_size(value, 0, &mut size).unwrap();
        size
    }

    #[test]
    fn validates_nested_objects_without_reordering_or_copying() {
        let mut fields = BTreeMap::new();
        fields.insert("b".into(), CanonValue::Bool(true));
        fields.insert("a".into(), CanonValue::Arr(vec![CanonValue::Int(-12)]));
        assert_eq!(validate_args(&CanonValue::Obj(fields)), Ok(()));
    }
}
