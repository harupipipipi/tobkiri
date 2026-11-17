//! Read the original active reservation for publication recovery.
use super::*;
use crate::original_authority::io::incomplete_reservations::{Row, own_row};
use crate::original_authority::io::{ActiveReservation, Database, Store};
use rusqlite::types::Value;

#[cfg(test)]
#[path = "original_recovery_active_reservation_tests.rs"]
mod tests;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::ActiveReservation;

pub(super) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    intent: &Intent,
) -> Result<bool> {
    let candidate = intent
        .doc
        .get(intent.payload, "candidate")
        .expect("intent payload");
    let activation_id = subscript(&intent.doc, candidate, "activation_id")?;
    let reservation = read_active(store, |db| {
        db.active_reservation(
            [],
            |query| {
                let id = bind(op, intent.doc.node(activation_id))?;
                query.bind_activation_id(&id.value)?;
                Ok(id)
            },
            |_| Ok(()),
        )
    })?;
    Ok(reservation.is_some())
}

/// Read the five fields consumed by snapshot validation, in comparison order.
/// An absent column remains distinct from a column containing SQL NULL.
pub(super) fn read_snapshot_binding<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    store: &Store<'_, '_, S>,
    activation_id: &str,
) -> Result<Option<Row<5>>> {
    read_active(store, |db| {
        db.active_reservation(
            [
                b"profile_id",
                b"plan_digest",
                b"profile_authority_digest",
                b"security_epoch",
                b"fencing_token",
            ],
            |query| {
                let capacity = op.reserve(STAGE, activation_id.len())?;
                query.bind_text(activation_id)?;
                Ok(capacity)
            },
            |row| own_row(op, STAGE, row),
        )
    })
}

fn read_active<S: OriginalAuthorityOwner, T>(
    store: &Store<'_, '_, S>,
    fetch: impl FnOnce(&Database<'_, '_, '_, S>) -> Result<ActiveReservation<T>>,
) -> Result<Option<T>> {
    let db = store
        .connection(STAGE)
        .map_err(|error| sqlite_read_error(error, "authority database is unavailable"))?;
    let reservation = fetch(&db);
    match db
        .complete(reservation)
        .map_err(|error| sqlite_read_error(error, "active activation read failed"))?
    {
        ActiveReservation::Absent => Ok(None),
        ActiveReservation::Sole(row) => Ok(Some(row)),
        ActiveReservation::Multiple => Err(OriginalAuthorityError::new(
            STAGE,
            "multiple active authority reservations exist",
            None,
        )),
    }
}

fn bind<S: OriginalAuthorityOwner>(op: &Operation<'_, S>, node: &Node) -> Result<Funded<Value>> {
    let bytes = match node {
        Node::String(text) => text
            .0
            .len()
            .checked_mul(4)
            .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
        _ => std::mem::size_of::<Value>(),
    };
    // SQLite owns a transient copy of a bound text parameter.
    let bound = bytes
        .checked_mul(2)
        .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
    let capacity = op.reserve(STAGE, bound)?;
    let value = match node {
        Node::String(text) => {
            // The signed document has already passed strict Unicode decoding.
            let mut output = String::with_capacity(bytes);
            for &point in &text.0 {
                output.push(char::from_u32(point).ok_or_else(|| {
                    OriginalAuthorityError::unsupported(STAGE, "invalid signed activation ID text")
                })?);
            }
            Value::Text(output)
        }
        Node::Scalar(CanonValue::Null) => Value::Null,
        Node::Scalar(CanonValue::Bool(value)) => Value::Integer(i64::from(*value)),
        Node::Scalar(CanonValue::Int(value)) => Value::Integer(*value),
        Node::Array(_) => {
            return Err(binding_error(
                "sqlite3.ProgrammingError",
                "Error binding parameter 1: type 'list' is not supported",
            ));
        }
        Node::Object(_) => {
            return Err(binding_error(
                "sqlite3.ProgrammingError",
                "Error binding parameter 1: type 'dict' is not supported",
            ));
        }
        // Other numeric representations cannot pass the signed journal's
        // strict decoder.
        Node::Scalar(_) => {
            return Err(OriginalAuthorityError::unsupported(
                STAGE,
                "activation ID binding type",
            ));
        }
    };
    Ok(Funded {
        value,
        _capacity: capacity,
    })
}

fn binding_error(class: &'static str, message: &'static str) -> OriginalAuthorityError {
    let mut error = raw(class, message);
    error.stage = STAGE;
    error
}

fn sqlite_read_error(
    error: OriginalAuthorityError,
    message: &'static str,
) -> OriginalAuthorityError {
    if error.class.starts_with("sqlite3.") {
        caused(
            OriginalAuthorityError::new(error.stage, message, None),
            error,
        )
    } else {
        error
    }
}
