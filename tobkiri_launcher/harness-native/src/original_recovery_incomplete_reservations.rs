//! The original fetchall before any activation recovery transitions.
use super::*;
use rusqlite::ffi;
use std::{ffi::CStr, ptr::NonNull};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::IncompleteReservation;
const SQL: &CStr = c"SELECT * FROM activation_reservations WHERE profile_id=? AND state IN ('prepared', 'ready_without_authority', 'committing') ORDER BY fencing_token";

// Only these dictionary entries are used by _recover_locked. Keep SQLite
// values intact until Python's subsequent str() calls are migrated. None means
// the original dictionary has no such key, which is not a fetchall error.
type Entry = [Option<Value>; 2]; // reservation_id, state

#[derive(Default)]
pub(in crate::original_authority::io) struct Inventory {
    rows: Vec<Funded<Entry>>,
    vector_capacity: Option<NativeSetupReservation>,
}
impl Inventory {
    pub(in crate::original_authority::io) fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }

    fn push<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
        values: [Option<ValueRef<'_>>; 2],
    ) -> Result<()> {
        let bytes = values
            .iter()
            .flatten()
            .try_fold(0usize, |acc, item| {
                acc.checked_add(match item {
                    ValueRef::Text(text) | ValueRef::Blob(text) => text.len(),
                    _ => 0,
                })
            })
            .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
        let capacity = op.reserve(STAGE, bytes)?;
        if self.rows.len() == self.rows.capacity() {
            let count = self
                .rows
                .capacity()
                .max(2)
                .checked_mul(2)
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
            let bytes = count
                .checked_mul(std::mem::size_of::<Funded<Entry>>())
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
            let permit = op.reserve(STAGE, bytes)?;
            self.rows
                .try_reserve_exact(count - self.rows.len())
                .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
            if self.rows.capacity() > count {
                return Err(OriginalAuthorityError::capacity(STAGE));
            }
            self.vector_capacity = Some(permit);
        }
        let [id, state] = values.map(|value| value.map(own).transpose());
        self.rows.push(Funded {
            value: [id?, state?],
            _capacity: capacity,
        });
        Ok(())
    }
}

pub(in crate::original_authority::io) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    profile_id: &str,
) -> Result<Inventory> {
    let db = store
        .connection(STAGE)
        .map_err(|error| wrap_sqlite(error, "authority database is unavailable"))?;
    let rows = fetch_all(op, &db, profile_id);
    db.complete(rows)
        .map_err(|error| wrap_sqlite(error, "activation recovery inventory failed"))
}

fn fetch_all<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    db: &Database<'_, '_, '_, S>,
    profile_id: &str,
) -> Result<Inventory> {
    db.validate()?;
    let connection = db.connection();
    let query = Query::prepare(connection)?;
    // Drop the SQLite transient copy before returning its capacity permit.
    let (_bind_capacity, mut query) = (op.reserve(STAGE, profile_id.len())?, query);
    query.bind_profile(profile_id)?;
    // CPython executes one step before it assembles the cursor description.
    let mut has_row = query.step()?;
    let fields = query.fields()?;
    let mut inventory = Inventory::default();
    while has_row {
        op.check(STAGE)?;
        inventory.push(op, query.convert_row(fields)?)?;
        has_row = query.step()?;
    }
    // Statement finalizes before Database::complete closes its connection.
    Ok(inventory)
}

fn own(value: ValueRef<'_>) -> Result<Value> {
    Ok(match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => Value::Integer(value),
        ValueRef::Real(value) => Value::Real(value),
        ValueRef::Blob(value) => {
            let mut bytes = Vec::new();
            bytes
                .try_reserve_exact(value.len())
                .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
            bytes.extend_from_slice(value);
            Value::Blob(bytes)
        }
        ValueRef::Text(value) => {
            let value = std::str::from_utf8(value).expect("already validated SQLite row text");
            let mut text = String::new();
            text.try_reserve_exact(value.len())
                .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
            text.push_str(value);
            Value::Text(text)
        }
    })
}

// Read name bytes without rusqlite::Statement::column_name, which panics if
// a legacy/corrupt schema cannot decode as UTF-8. The one constant statement
// stays on the guarded connection and the handle cannot escape this module.
struct Query<'a> {
    connection: &'a Connection,
    statement: NonNull<ffi::sqlite3_stmt>,
}
impl<'a> Query<'a> {
    fn prepare(connection: &'a Connection) -> Result<Self> {
        let mut statement = std::ptr::null_mut();
        // SAFETY: The connection is live; SQL is a static, NUL-terminated C
        // string. SQLite sets the statement pointer to null on failure.
        let rc = unsafe {
            ffi::sqlite3_prepare_v2(
                connection.handle(),
                SQL.as_ptr(),
                -1,
                &mut statement,
                std::ptr::null_mut(),
            )
        };
        if rc != ffi::SQLITE_OK {
            return Err(sql_error(connection, rc));
        }
        Ok(Self {
            connection,
            statement: NonNull::new(statement).expect("fixed SQL is a statement"),
        })
    }

    fn bind_profile(&self, profile_id: &str) -> Result<()> {
        let size = i32::try_from(profile_id.len())
            .map_err(|_| raw("builtins.OverflowError", "string longer than INT_MAX bytes"))?;
        // SAFETY: Statement is live during this call. SQLite immediately owns
        // its funded transient copy, including any interior NUL in the ID.
        let rc = unsafe {
            ffi::sqlite3_bind_text(
                self.statement.as_ptr(),
                1,
                profile_id.as_ptr().cast(),
                size,
                ffi::SQLITE_TRANSIENT(),
            )
        };
        if rc == ffi::SQLITE_OK {
            Ok(())
        } else {
            Err(sql_error(self.connection, rc))
        }
    }

    fn step(&mut self) -> Result<bool> {
        // SAFETY: The statement is live, and &mut self excludes borrowed
        // pointers from the preceding step.
        match unsafe { ffi::sqlite3_step(self.statement.as_ptr()) } {
            ffi::SQLITE_ROW => Ok(true),
            ffi::SQLITE_DONE => Ok(false),
            rc => Err(sql_error(self.connection, rc)),
        }
    }

    fn columns(&self) -> i32 {
        // SAFETY: The statement is live.
        unsafe { ffi::sqlite3_column_count(self.statement.as_ptr()) }
    }

    fn fields(&self) -> Result<[Option<i32>; 2]> {
        let mut fields = [None; 2];
        for column in 0..self.columns() {
            let name = self.name(column)?;
            if let Err(error) = name.to_str() {
                return Err(invalid_utf8(name.to_bytes(), error));
            }
            match name.to_bytes() {
                b"reservation_id" => fields[0] = Some(column),
                b"state" => fields[1] = Some(column),
                _ => (),
            }
        }
        Ok(fields)
    }

    fn convert_row(&self, fields: [Option<i32>; 2]) -> Result<[Option<ValueRef<'_>>; 2]> {
        let mut selected = [None; 2];
        for column in 0..self.columns() {
            let value = self.value(column)?;
            if let ValueRef::Text(text) = value
                && std::str::from_utf8(text).is_err()
            {
                return Err(invalid_cell_text(self.name(column)?.to_bytes(), text));
            }
            for (index, field) in fields.iter().enumerate() {
                if *field == Some(column) {
                    selected[index] = Some(value);
                }
            }
        }
        Ok(selected)
    }

    fn name(&self, column: i32) -> Result<&CStr> {
        // SAFETY: Indices come from this statement's column count. Names stay
        // live until the next step/finalize, both requiring exclusive ownership.
        let pointer = unsafe { ffi::sqlite3_column_name(self.statement.as_ptr(), column) };
        if pointer.is_null() {
            return Err(raw("builtins.MemoryError", ""));
        }
        Ok(unsafe { CStr::from_ptr(pointer) })
    }

    fn value(&self, column: i32) -> Result<ValueRef<'_>> {
        let statement = self.statement.as_ptr();
        // SAFETY: This is a current row with a valid index. Establish the type
        // before reading and do not request a different SQLite representation.
        unsafe {
            Ok(match ffi::sqlite3_column_type(statement, column) {
                ffi::SQLITE_NULL => ValueRef::Null,
                ffi::SQLITE_INTEGER => {
                    ValueRef::Integer(ffi::sqlite3_column_int64(statement, column))
                }
                ffi::SQLITE_FLOAT => ValueRef::Real(ffi::sqlite3_column_double(statement, column)),
                kind => {
                    let pointer = if kind == ffi::SQLITE_TEXT {
                        ffi::sqlite3_column_text(statement, column)
                    } else {
                        ffi::sqlite3_column_blob(statement, column).cast()
                    };
                    if pointer.is_null()
                        && ffi::sqlite3_errcode(self.connection.handle()) == ffi::SQLITE_NOMEM
                    {
                        return Err(raw("builtins.MemoryError", ""));
                    }
                    let len = ffi::sqlite3_column_bytes(statement, column);
                    let bytes = if len > 0 && !pointer.is_null() {
                        std::slice::from_raw_parts(pointer, len as usize)
                    } else {
                        &[]
                    };
                    if kind == ffi::SQLITE_TEXT {
                        ValueRef::Text(bytes)
                    } else {
                        ValueRef::Blob(bytes)
                    }
                }
            })
        }
    }
}
impl Drop for Query<'_> {
    fn drop(&mut self) {
        // SAFETY: This uniquely owned handle is finalized once. If SQLite
        // reports a prior execution error, step already captured that error.
        unsafe { ffi::sqlite3_finalize(self.statement.as_ptr()) };
    }
}

fn sql_error(connection: &Connection, rc: i32) -> OriginalAuthorityError {
    if rc & 0xff == ffi::SQLITE_NOMEM {
        return raw("builtins.MemoryError", "");
    }
    // SAFETY: The connection stays live and no intervening SQLite operation
    // invalidates the error string while it is copied.
    let (message, code) = unsafe {
        let handle = connection.handle();
        let code = ffi::sqlite3_extended_errcode(handle);
        let message = CStr::from_ptr(ffi::sqlite3_errmsg(handle));
        match message.to_str() {
            Ok(message) => (message.to_owned(), code),
            Err(error) => return invalid_utf8(message.to_bytes(), error),
        }
    };
    OriginalAuthorityError::raw_sql(
        STAGE,
        rusqlite::Error::SqliteFailure(
            ffi::Error::new(if code & 0xff == rc & 0xff { code } else { rc }),
            Some(message),
        ),
    )
}

fn raw(class: &'static str, message: impl Into<String>) -> OriginalAuthorityError {
    let mut error = json::raw_error(class, message);
    error.stage = STAGE;
    error
}

fn invalid_cell_text(column: &[u8], text: &[u8]) -> OriginalAuthorityError {
    // CPython's 200-byte buffer passes 199 to snprintf, then decodes ASCII
    // with replacement; both C string arguments terminate at their first NUL.
    let text = text.split(|&byte| byte == 0).next().unwrap_or_default();
    let message: String = [
        b"Could not decode to UTF-8 column '".as_slice(),
        column,
        b"' with text '",
        text,
        b"'",
    ]
    .into_iter()
    .flatten()
    .take(198)
    .map(|&byte| {
        if byte.is_ascii() {
            char::from(byte)
        } else {
            '\u{fffd}'
        }
    })
    .collect();
    raw("sqlite3.OperationalError", message)
}

fn invalid_utf8(bytes: &[u8], error: std::str::Utf8Error) -> OriginalAuthorityError {
    let start = error.valid_up_to();
    let (size, reason) = match error.error_len() {
        None => (bytes.len() - start, "unexpected end of data"),
        Some(size) => (
            size,
            if (0xc2..=0xf4).contains(&bytes[start]) {
                "invalid continuation byte"
            } else {
                "invalid start byte"
            },
        ),
    };
    let message = if size == 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            bytes[start]
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
            start + size - 1
        )
    };
    raw("builtins.UnicodeDecodeError", message)
}

fn wrap_sqlite(error: OriginalAuthorityError, message: &'static str) -> OriginalAuthorityError {
    if !error.class.starts_with("sqlite3.") {
        return error;
    }
    OriginalAuthorityError::new(error.stage, message, Some(publication::into_cause(error)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn connection() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE activation_reservations (
                      audit_data, state, fencing_token, profile_id, reservation_id
                   );",
            )
            .unwrap();
        connection
    }

    #[test]
    fn inventory_query_preserves_fencing_order_and_id_types() {
        let connection = connection();
        connection
            .execute_batch(
                "INSERT INTO activation_reservations VALUES
                    ('ok', 'committing', 11, CAST(x'700071' AS TEXT), x'00fe'),
                    ('ok', 'prepared', 3, CAST(x'700071' AS TEXT), 999),
                    ('ok', 'ready_without_authority', 20, CAST(x'700071' AS TEXT), 'last'),
                    ('ok', 'retired', 2, CAST(x'700071' AS TEXT), 'retired'),
                    ('ok', 'active', 1, CAST(x'700071' AS TEXT), 'active'),
                    ('ok', 'prepared', 4, 'p', 'different-profile');",
            )
            .unwrap();
        let mut query = Query::prepare(&connection).unwrap();
        query.bind_profile("p\0q").unwrap();
        assert!(query.step().unwrap());
        let fields = query.fields().unwrap();
        for (id, state) in [
            (ValueRef::Integer(999), b"prepared".as_slice()),
            (ValueRef::Blob(b"\0\xfe"), b"committing"),
            (ValueRef::Text(b"last"), b"ready_without_authority"),
        ] {
            assert_eq!(
                query.convert_row(fields).unwrap(),
                [Some(id), Some(ValueRef::Text(state))]
            );
            if state != b"ready_without_authority" {
                assert!(query.step().unwrap());
            }
        }
        assert!(!query.step().unwrap());
        drop(query);
        connection.close().unwrap();
    }

    #[test]
    fn inventory_checks_later_invalid_text_even_in_an_unused_column() {
        let connection = connection();
        connection
            .execute_batch(
                "INSERT INTO activation_reservations VALUES
                    (CAST(x'61ff0062' AS TEXT), 'prepared', 4, 'p', 'invalid'),
                    ('ok', 'prepared', 3, 'p', 'valid');",
            )
            .unwrap();
        let mut query = Query::prepare(&connection).unwrap();
        query.bind_profile("p").unwrap();
        assert!(query.step().unwrap());
        let fields = query.fields().unwrap();
        assert_eq!(
            query.convert_row(fields).unwrap()[0],
            Some(ValueRef::Text(b"valid"))
        );
        assert!(query.step().unwrap());
        let error = wrap_sqlite(
            query.convert_row(fields).unwrap_err(),
            "activation recovery inventory failed",
        );
        assert_eq!(error.stage, STAGE);
        assert_eq!(error.message, "activation recovery inventory failed");
        assert!(matches!(
            error.cause,
            Some(OriginalAuthorityCause::Python {
                class: "sqlite3.OperationalError",
                message,
                ..
            }) if message == "Could not decode to UTF-8 column 'audit_data' with text 'a�'"
        ));
        drop(query);
        connection.close().unwrap();
    }

    #[test]
    fn inventory_preserves_original_error_for_invalid_utf8_in_sqlite_diagnostic() {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE VIEW activation_reservations AS
                 SELECT json_extract('{}', CAST(x'ff' AS TEXT)) AS audit_data,
                        'prepared' AS state, 1 AS fencing_token,
                        'p' AS profile_id, 'r' AS reservation_id;",
            )
            .unwrap();
        let mut query = Query::prepare(&connection).unwrap();
        query.bind_profile("p").unwrap();
        let error = wrap_sqlite(
            query.step().unwrap_err(),
            "activation recovery inventory failed",
        );
        assert_eq!(error.stage, STAGE);
        assert_eq!(error.class, "builtins.UnicodeDecodeError");
        assert_eq!(
            error.message,
            "'utf-8' codec can't decode byte 0xff in position 16: invalid start byte"
        );
        assert!(error.cause.is_none());
    }
}
