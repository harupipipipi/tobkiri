//! The missing-workspace caller consumes only the reservation's presence.
use super::*;
use crate::original_authority::io::Store;
use rusqlite::types::Value;

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
    let db = store
        .connection(STAGE)
        .map_err(|error| sqlite_read_error(error, "authority database is unavailable"))?;
    let count = db.active_reservation_count(|| bind(op, intent.doc.node(activation_id)));
    match db
        .complete(count)
        .map_err(|error| sqlite_read_error(error, "active activation read failed"))?
    {
        0 => Ok(false),
        1 => Ok(true),
        _ => Err(OriginalAuthorityError::new(
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
