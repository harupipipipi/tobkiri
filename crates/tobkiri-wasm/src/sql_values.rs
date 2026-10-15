//! Exact generic Core1.1 SQL cell wire encoding. Core1.0 is a separate adapter.
use std::collections::BTreeMap;
use tobkiri_canon::CanonValue;
use tobkiri_store::{SqlCell, SqlInteger, SqlReal, StoreError};

pub(crate) fn decode_cell(value: &CanonValue) -> Result<SqlCell, StoreError> {
    let CanonValue::Obj(fields) = value else {
        return Err(StoreError::Invalid(
            "SQL cell must be a typed envelope".into(),
        ));
    };
    if fields.len() != 2 || !fields.contains_key("type") || !fields.contains_key("value") {
        return Err(StoreError::Invalid(
            "SQL cell requires exactly type and value".into(),
        ));
    }
    let CanonValue::Str(kind) = &fields["type"] else {
        return Err(StoreError::Invalid("SQL cell type must be a string".into()));
    };
    let value = &fields["value"];
    Ok(match (kind.as_str(), value) {
        ("null", CanonValue::Null) => SqlCell::Null,
        ("integer", CanonValue::Str(value)) => SqlCell::Integer(SqlInteger::parse(value)?),
        ("real", CanonValue::Str(value)) => SqlCell::Real(SqlReal::parse(value)?),
        ("text", CanonValue::Str(value)) => SqlCell::Text(value.clone()),
        ("blob", CanonValue::Arr(values)) => SqlCell::Blob(
            values
                .iter()
                .map(|value| match value {
                    CanonValue::Int(value) => u8::try_from(*value)
                        .map_err(|_| StoreError::Invalid("SQL blob byte outside u8".into())),
                    _ => Err(StoreError::Invalid("SQL blob contains a non-byte".into())),
                })
                .collect::<Result<_, _>>()?,
        ),
        _ => {
            return Err(StoreError::Invalid(
                "unknown SQL cell tag or invalid tagged value".into(),
            ));
        }
    })
}
pub(crate) fn encode_cell(cell: &SqlCell) -> CanonValue {
    let (kind, value) = match cell {
        SqlCell::Null => ("null", CanonValue::Null),
        SqlCell::Integer(value) => ("integer", CanonValue::Str(value.as_str().into())),
        SqlCell::Real(value) => ("real", CanonValue::Str(value.as_str().into())),
        SqlCell::Text(value) => ("text", CanonValue::Str(value.clone())),
        SqlCell::Blob(values) => (
            "blob",
            CanonValue::Arr(
                values
                    .iter()
                    .map(|byte| CanonValue::Int(i64::from(*byte)))
                    .collect(),
            ),
        ),
    };
    CanonValue::Obj(BTreeMap::from([
        ("type".into(), CanonValue::Str(kind.into())),
        ("value".into(), value),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lossless_width_real_sign_blob_unicode_nul_and_null_remain_distinct() {
        for cell in [
            SqlCell::Null,
            SqlCell::Integer(SqlInteger::new(i64::MIN)),
            SqlCell::Integer(SqlInteger::new(i64::MAX)),
            SqlCell::Real(SqlReal::new(-0.0).unwrap()),
            SqlCell::Real(SqlReal::new(f64::MAX).unwrap()),
            SqlCell::Text("nul\0日本語".into()),
            SqlCell::Blob(vec![0, 128, 255]),
        ] {
            assert_eq!(decode_cell(&encode_cell(&cell)).unwrap(), cell);
        }
        assert_ne!(
            encode_cell(&SqlCell::Null),
            encode_cell(&SqlCell::Text("null".into()))
        );
    }
    #[test]
    fn coercion_noncanonical_numbers_missing_fields_and_extras_are_rejected() {
        let envelope = |kind: &str, value: CanonValue| {
            CanonValue::Obj(BTreeMap::from([
                ("type".into(), CanonValue::Str(kind.into())),
                ("value".into(), value),
            ]))
        };
        for invalid in [
            envelope("integer", CanonValue::Int(1)),
            envelope("integer", CanonValue::Str("+1".into())),
            envelope("integer", CanonValue::Str("01".into())),
            envelope("real", CanonValue::Str("-0.0".into())),
            envelope("real", CanonValue::Str("NaN".into())),
            envelope("int", CanonValue::Str("1".into())),
            envelope("null", CanonValue::Str("null".into())),
            envelope("blob", CanonValue::Arr(vec![CanonValue::Int(256)])),
            CanonValue::Obj(BTreeMap::from([(
                "type".into(),
                CanonValue::Str("null".into()),
            )])),
        ] {
            assert!(decode_cell(&invalid).is_err());
        }
        let CanonValue::Obj(mut extra) = envelope("null", CanonValue::Null) else {
            unreachable!()
        };
        extra.insert("other".into(), CanonValue::Null);
        assert!(decode_cell(&CanonValue::Obj(extra)).is_err());
    }
}
