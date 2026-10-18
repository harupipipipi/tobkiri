//! The Run repository uses the existing private typed SQL/Gate boundary.

use crate::run_repository::{
    SqlCell, SqlDatabase, SqlError, SqlErrorKind, SqlRows, SqlTransaction,
};
use crate::tobkiri::core1_1_0::{
    storage,
    types::{Error, ErrorCode},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::rc::Rc;

pub struct CoreDatabase(pub Rc<storage::Store>);

pub struct CoreTransaction {
    transaction: storage::SqlTransaction,
    // Keep the ordinary parent resource alive until its transaction is closed.
    _store: Rc<storage::Store>,
}

fn failed(error: Error) -> SqlError {
    SqlError::new(match error.code {
        ErrorCode::Conflict => SqlErrorKind::Conflict,
        ErrorCode::Denied | ErrorCode::ApprovalRequired => SqlErrorKind::Denied,
        ErrorCode::Cancelled => SqlErrorKind::Cancelled,
        ErrorCode::LimitExceeded => SqlErrorKind::Limit,
        ErrorCode::Unavailable | ErrorCode::NotFound => SqlErrorKind::Unavailable,
        ErrorCode::Invalid => SqlErrorKind::Invalid,
        ErrorCode::Internal => SqlErrorKind::Internal,
    })
}

fn invalid() -> SqlError {
    SqlError::new(SqlErrorKind::Invalid)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Cell {
    #[serde(rename = "type")]
    kind: String,
    value: Value,
}

fn encode(cell: &SqlCell) -> String {
    match cell {
        SqlCell::Null => json!({"type":"null","value":null}),
        SqlCell::Integer(value) => json!({"type":"integer","value":value.to_string()}),
        SqlCell::Text(value) => json!({"type":"text","value":value}),
        SqlCell::Blob(value) => json!({"type":"blob","value":value}),
    }
    .to_string()
}

fn decode(text: &str) -> Result<SqlCell, SqlError> {
    // A blob's decimal-array wire representation is at most four times its
    // bytes. Bound before serde allocation, independently of Core's bounds.
    if text.len() > 4 * crate::run_repository::CHUNK_BYTES + 256 {
        return Err(SqlError::new(SqlErrorKind::Limit));
    }
    let cell: Cell = serde_json::from_str(text).map_err(|_| invalid())?;
    match (cell.kind.as_str(), cell.value) {
        ("null", Value::Null) => Ok(SqlCell::Null),
        ("text", Value::String(value)) => Ok(SqlCell::Text(value)),
        ("integer", Value::String(text)) => {
            let value = text.parse::<i64>().map_err(|_| invalid())?;
            if value.to_string() != text {
                return Err(invalid());
            }
            Ok(SqlCell::Integer(value))
        }
        ("blob", Value::Array(bytes)) => {
            if bytes.len() > crate::run_repository::CHUNK_BYTES {
                return Err(SqlError::new(SqlErrorKind::Limit));
            }
            bytes
                .into_iter()
                .map(|value| {
                    value
                        .as_u64()
                        .and_then(|value| u8::try_from(value).ok())
                        .ok_or_else(invalid)
                })
                .collect::<Result<Vec<_>, _>>()
                .map(SqlCell::Blob)
        }
        _ => Err(invalid()),
    }
}

fn affected(value: Option<String>) -> Result<u64, SqlError> {
    let text = value.ok_or_else(invalid)?;
    let count = text.parse::<u64>().map_err(|_| invalid())?;
    if count.to_string() != text {
        return Err(invalid());
    }
    Ok(count)
}

impl SqlDatabase for CoreDatabase {
    type Transaction = CoreTransaction;

    fn begin(&mut self) -> Result<Self::Transaction, SqlError> {
        let transaction = self.0.sql_begin().map_err(failed)?;
        Ok(CoreTransaction {
            transaction,
            _store: Rc::clone(&self.0),
        })
    }
}

impl SqlTransaction for CoreTransaction {
    fn exec(&mut self, statement: &str, params: &[SqlCell]) -> Result<u64, SqlError> {
        let parameters = params.iter().map(encode).collect::<Vec<_>>();
        let result = self
            .transaction
            .sql_exec_typed(
                statement,
                &parameters,
                0,
                storage::SqlResultPolicy::ForbidRows,
            )
            .map_err(failed)?;
        if !result.columns.is_empty() || !result.rows.is_empty() {
            return Err(invalid());
        }
        affected(result.affected_rows)
    }

    fn query(
        &mut self,
        statement: &str,
        params: &[SqlCell],
        max_rows: u32,
    ) -> Result<SqlRows, SqlError> {
        let parameters = params.iter().map(encode).collect::<Vec<_>>();
        let result = self
            .transaction
            .sql_query_typed(statement, &parameters, max_rows)
            .map_err(failed)?;
        if result.affected_rows.is_some()
            || result.last_insert_rowid.is_some()
            || result.rows.len() > max_rows as usize
            || result
                .rows
                .iter()
                .any(|row| row.len() != result.columns.len())
        {
            return Err(invalid());
        }
        let rows = result
            .rows
            .into_iter()
            .map(|row| row.iter().map(|cell| decode(cell)).collect())
            .collect::<Result<Vec<Vec<_>>, _>>()?;
        Ok(SqlRows {
            columns: result.columns,
            rows,
        })
    }

    fn commit(self) -> Result<(), SqlError> {
        // WIT has one public error class for several native failure points.
        // Any unconfirmed commit loses dispatch eligibility; readback cannot
        // recreate a permit even if it later finds the committed claim.
        self.transaction
            .commit()
            .map_err(|_| SqlError::commit_unknown())
    }

    fn rollback(self) -> Result<(), SqlError> {
        self.transaction
            .rollback()
            .map_err(|_| SqlError::commit_unknown())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typed_cells_preserve_full_i64_binary_and_unicode() {
        for cell in [
            SqlCell::Null,
            SqlCell::Integer(i64::MIN),
            SqlCell::Integer(i64::MAX),
            SqlCell::Text("猫\n\"\\".into()),
            SqlCell::Blob((0..=255).collect()),
        ] {
            assert_eq!(decode(&encode(&cell)).unwrap(), cell);
        }
    }

    #[test]
    fn typed_cells_reject_duplicate_fields_noncanonical_integer_and_bad_bytes() {
        for text in [
            r#"{"type":"integer","value":"00"}"#,
            r#"{"type":"integer","value":"-0"}"#,
            r#"{"type":"integer","value":9007199254740993}"#,
            r#"{"type":"text","value":"a","value":"b"}"#,
            r#"{"type":"blob","value":[256]}"#,
            r#"{"type":"blob","value":[-1]}"#,
            r#"{"type":"blob","value":[1.0]}"#,
            r#"{"type":"blob","value":["1"]}"#,
            r#"{"type":"null","value":0}"#,
            r#"{"type":"text","value":"x","extra":true}"#,
        ] {
            assert!(decode(text).is_err(), "{text}");
        }
        assert!(affected(None).is_err());
        assert!(affected(Some("01".into())).is_err());
        assert_eq!(affected(Some(u64::MAX.to_string())).unwrap(), u64::MAX);
    }

    #[test]
    fn maximum_chunk_round_trips_and_larger_chunk_is_rejected() {
        let cell = SqlCell::Blob(vec![255; crate::run_repository::CHUNK_BYTES]);
        assert_eq!(decode(&encode(&cell)).unwrap(), cell);
        let large = SqlCell::Blob(vec![0; crate::run_repository::CHUNK_BYTES + 1]);
        assert!(decode(&encode(&large)).is_err());
    }
}
