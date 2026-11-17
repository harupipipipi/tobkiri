//! The original activation reservation and audited CAS transaction closure.
use super::incomplete_reservations::{
    EPOCH_SQL, PENDING_SQL, Query, fetch_one, own_row, wrap_sqlite,
};
use super::*;
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use rusqlite::params;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::ActivationTransition;
const ID_PREFIX: &str = "activation-reservation:";
const FIELDS: [&str; 8] = [
    "reservation_id",
    "activation_id",
    "profile_id",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
    "state",
];
const TOKEN_SQL: &std::ffi::CStr =
    c"SELECT value FROM authority_meta WHERE key='activation_fencing_token'";
const DUPLICATE_SQL: &std::ffi::CStr = c"SELECT 1 FROM activation_reservations WHERE activation_id=? AND state NOT IN ('aborted', 'retired')";
const SUPERSEDED_SQL: &std::ffi::CStr = c"SELECT reservation_id, activation_id FROM activation_reservations WHERE profile_id=? AND state='active' AND reservation_id<>?";

pub(super) struct ReservationInput<'a> {
    pub activation_id: &'a str,
    pub profile_id: &'a str,
    pub plan_digest: &'a str,
    pub profile_authority_digest: &'a str,
    pub security_epoch: i64,
}

pub(super) struct Reservation {
    pub reservation_id: String,
    pub fencing_token: i64,
}

/// Reserve one activation with the original never-reused fencing sequence.
pub(super) fn reserve<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    input: ReservationInput<'_>,
) -> Result<Funded<Reservation>> {
    store.check_process(STAGE)?;
    let op = store.op;
    let capacity = op.reserve(
        STAGE,
        ID_PREFIX.len() + 32 + std::mem::size_of::<Reservation>(),
    )?;
    let mut random = [0; 24];
    crypto::random(&mut random, STAGE)?;
    let mut encoded = [0; 32];
    URL_SAFE_NO_PAD
        .encode_slice(random, &mut encoded)
        .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
    let mut reservation_id = String::with_capacity(ID_PREFIX.len() + encoded.len());
    reservation_id.push_str(ID_PREFIX);
    reservation_id.push_str(std::str::from_utf8(&encoded).expect("base64 is ASCII"));
    let db = store
        .connection(STAGE)
        .map_err(|e| wrap_sqlite(e, "authority database is unavailable"))?;
    let result = (|| {
        db.execute("BEGIN IMMEDIATE", [])?;
        // The two rows are fetched before either is inspected in Python.
        let epoch = fetch_one(op, &db, STAGE, EPOCH_SQL, None, ["value"])?;
        let token = fetch_one(op, &db, STAGE, TOKEN_SQL, None, ["value"])?;
        let Some(epoch) = epoch else {
            return Err(denied("activation has a stale SecurityEpoch"));
        };
        let epoch = integer_value(op, field(&epoch.value, 0)?)?;
        let _comparison = op.reserve(STAGE, 32)?;
        if epoch.value != input.security_epoch.to_string() {
            return Err(denied("activation has a stale SecurityEpoch"));
        }
        let token = token.ok_or_else(|| {
            OriginalAuthorityError::new(STAGE, "activation fencing token is unavailable", None)
        })?;
        let mut token = integer_value(op, field(&token.value, 0)?)?;
        increment(&mut token.value);
        let now = store.clock();
        if fetch_one(op, &db, STAGE, DUPLICATE_SQL, Some(input.activation_id), [])?.is_some() {
            return Err(denied("activation identity is already reserved"));
        }
        // Python adds with arbitrary precision, then SQLite's parameter
        // adapter rejects an out-of-range integer at the INSERT boundary.
        let fencing_token = token.value.parse::<i64>().map_err(|_| {
            raw(
                "builtins.OverflowError",
                "Python int too large to convert to SQLite INTEGER",
            )
        })?;
        let binding_bytes = [
            reservation_id.len(),
            input.activation_id.len(),
            input.profile_id.len(),
            input.plan_digest.len(),
            input.profile_authority_digest.len(),
            token.value.len(),
            256,
        ]
        .into_iter()
        .try_fold(0usize, usize::checked_add)
        .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
        let _binding = op.reserve(STAGE, binding_bytes)?;
        db.execute(
            "INSERT INTO activation_reservations (reservation_id, activation_id, profile_id, plan_digest, profile_authority_digest, security_epoch, fencing_token, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![reservation_id, input.activation_id, input.profile_id, input.plan_digest, input.profile_authority_digest, input.security_epoch, fencing_token, "prepared", now, now],
        )?;
        db.execute(
            "UPDATE authority_meta SET value=? WHERE key='activation_fencing_token'",
            [&token.value],
        )?;
        let event_id_size = "activation-prepared-".len() + reservation_id.len();
        let _payload = op.reserve(
            STAGE,
            binding_bytes
                .checked_add(event_id_size + 2048)
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
        )?;
        let event_id = format!("activation-prepared-{reservation_id}");
        schema::append_audit(
            store,
            &db,
            schema::AuditKind::Activation,
            &event_id,
            "prepared",
            object([
                ("reservation_id", CanonValue::Str(reservation_id.clone())),
                ("activation_id", CanonValue::Str(input.activation_id.into())),
                ("profile_id", CanonValue::Str(input.profile_id.into())),
                ("plan_digest", CanonValue::Str(input.plan_digest.into())),
                (
                    "profile_authority_digest",
                    CanonValue::Str(input.profile_authority_digest.into()),
                ),
                ("security_epoch", CanonValue::Int(input.security_epoch)),
                ("fencing_token", CanonValue::Int(fencing_token)),
            ]),
        )?;
        db.commit()?;
        Ok(fencing_token)
    })();
    let fencing_token = db.complete(result).map_err(|error| {
        if error.class == "sqlite3.IntegrityError" {
            let mut denied = denied("activation identity was already reserved");
            denied.cause = Some(publication::into_cause(error));
            denied.suppressed = true;
            denied
        } else {
            wrap_sqlite(error, "activation reservation failed")
        }
    })?;
    Ok(Funded {
        value: Reservation {
            reservation_id,
            fencing_token,
        },
        _capacity: capacity,
    })
}

/// CAS one activation state and append its authoritative audit atomically.
pub(super) fn transition<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    reservation_id: &str,
    expected_state: &str,
    new_state: &str,
) -> Result<()> {
    store.check_process(STAGE)?;
    if !matches!(
        (expected_state, new_state),
        ("prepared", "ready_without_authority" | "aborted")
            | ("ready_without_authority", "committing" | "aborted")
            | ("committing", "active" | "aborted")
            | ("active", "retired")
    ) {
        return Err(raw(
            "builtins.ValueError",
            "invalid activation state transition",
        ));
    }
    let op = store.op;
    let db = store
        .connection(STAGE)
        .map_err(|e| wrap_sqlite(e, "authority database is unavailable"))?;
    let result = (|| {
        db.execute("BEGIN IMMEDIATE", [])?;
        let mut row = fetch_one(op, &db, STAGE, PENDING_SQL, Some(reservation_id), FIELDS)?
            .ok_or_else(|| denied("activation reservation lost its state fence"))?;
        if value_text(field(&row.value, 7)?) != Some(expected_state) {
            return Err(denied("activation reservation lost its state fence"));
        }
        let epoch = fetch_one(op, &db, STAGE, EPOCH_SQL, None, ["value"])?;
        if !matches!(new_state, "aborted" | "retired") {
            let Some(epoch) = epoch else {
                return Err(denied("activation reservation has a stale SecurityEpoch"));
            };
            let epoch = integer_value(op, field(&epoch.value, 0)?)?;
            let _comparison = op.reserve(STAGE, 1024)?;
            if !integer_equals(&epoch.value, field(&row.value, 5)?) {
                return Err(denied("activation reservation has a stale SecurityEpoch"));
            }
        }
        let _binding = op.reserve(
            STAGE,
            reservation_id
                .len()
                .checked_add(expected_state.len() + new_state.len())
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
        )?;
        if db.execute(
            "UPDATE activation_reservations SET state=?, updated_at=? WHERE reservation_id=? AND state=?",
            params![new_state, store.clock(), reservation_id, expected_state],
        )? != 1
        {
            return Err(denied("activation transition lost a race"));
        }
        // The Python payload dictionary resolves all seven keys before
        // supersession, but does not serialize their values until the audit.
        for index in 0..7 {
            field(&row.value, index)?;
        }
        let _payload_capacity = op.reserve(STAGE, 2048)?;
        if new_state == "active" {
            let profile_id = field(&row.value, 2)?;
            let superseded = superseded(op, &db, profile_id, reservation_id)?;
            let _bindings = op.reserve(
                STAGE,
                binding_bytes(profile_id)
                    .checked_add(reservation_id.len() + "retired".len())
                    .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
            )?;
            db.execute(
                "UPDATE activation_reservations SET state='retired', updated_at=? WHERE profile_id=? AND state='active' AND reservation_id<>?",
                params![store.clock(), profile_id, reservation_id],
            )?;
            for mut old in superseded.rows {
                let old_id = old.value[0].take().ok_or_else(missing_column)?;
                let old_activation = old.value[1].take().ok_or_else(missing_column)?;
                let old_id_text = python_id(op, &old_id)?;
                let id_size = "activation-retired-".len() + old_id_text.len();
                let _audit = op.reserve(STAGE, id_size + reservation_id.len() + 2048)?;
                let event_id = format!("activation-retired-{}", old_id_text.value);
                schema::append_audit(
                    store,
                    &db,
                    schema::AuditKind::Activation,
                    &event_id,
                    "retired",
                    object([
                        ("reservation_id", canonical(old_id)?),
                        ("activation_id", canonical(old_activation)?),
                        ("superseded_by", CanonValue::Str(reservation_id.into())),
                    ]),
                )?;
            }
        }
        let event_id_size = "activation--".len() + new_state.len() + reservation_id.len();
        let _event_id = op.reserve(STAGE, event_id_size)?;
        let event_id = format!("activation-{new_state}-{reservation_id}");
        // Moved strings remain funded by the row through the audit append.
        let mut payload = BTreeMap::new();
        for (name, value) in FIELDS[..7].iter().zip(&mut row.value[..7]) {
            payload.insert(
                (*name).into(),
                canonical(value.take().ok_or_else(missing_column)?)?,
            );
        }
        schema::append_audit(
            store,
            &db,
            schema::AuditKind::Activation,
            &event_id,
            new_state,
            CanonValue::Obj(payload),
        )?;
        db.commit()
    })();
    db.complete(result)
        .map_err(|e| wrap_sqlite(e, "activation transition failed"))
}

struct Superseded {
    rows: Vec<Funded<[Option<Value>; 2]>>,
    _capacity: Option<NativeSetupReservation>,
}

fn superseded<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    db: &Database<'_, '_, '_, S>,
    profile_id: &Value,
    reservation_id: &str,
) -> Result<Superseded> {
    db.validate()?;
    let _binding = op.reserve(
        STAGE,
        binding_bytes(profile_id)
            .checked_add(reservation_id.len())
            .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
    )?;
    let mut query = Query::prepare(db.connection(), SUPERSEDED_SQL, STAGE)?;
    query.bind_activation_id(profile_id)?;
    query.bind_text_at(2, reservation_id)?;
    let mut has_row = query.step()?;
    let fields = query.fields([b"reservation_id".as_slice(), b"activation_id".as_slice()])?;
    let mut result = Superseded {
        rows: Vec::new(),
        _capacity: None,
    };
    while has_row {
        op.check(STAGE)?;
        if result.rows.len() == result.rows.capacity() {
            let count = result
                .rows
                .capacity()
                .max(2)
                .checked_mul(2)
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
            let capacity = op.reserve(
                STAGE,
                count
                    .checked_mul(std::mem::size_of::<Funded<[Option<Value>; 2]>>())
                    .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
            )?;
            result
                .rows
                .try_reserve_exact(count - result.rows.len())
                .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
            if result.rows.capacity() > count {
                return Err(OriginalAuthorityError::capacity(STAGE));
            }
            result._capacity = Some(capacity);
        }
        result
            .rows
            .push(own_row(op, STAGE, query.convert_row(fields)?)?);
        has_row = query.step()?;
    }
    Ok(result)
}

fn field<const N: usize>(row: &[Option<Value>; N], index: usize) -> Result<&Value> {
    row[index].as_ref().ok_or_else(missing_column)
}

fn integer_value<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    value: &Value,
) -> Result<Funded<String>> {
    let bytes = match value {
        Value::Text(text) => text.len().saturating_mul(10).saturating_add(256),
        Value::Blob(bytes) => bytes.len().saturating_mul(10).saturating_add(256),
        _ => 1024,
    };
    let capacity = op.reserve(STAGE, bytes)?;
    let value = match value {
        Value::Text(text) => {
            integer(text, STAGE)
                .map_err(|mut error| {
                    if error.message.starts_with("invalid literal for int()") {
                        // CPython applies %.200R to the completed repr; its
                        // closing quote can therefore be outside the limit.
                        error.message = format!(
                            "invalid literal for int() with base 10: {}",
                            integer_repr(text)
                        );
                    }
                    error
                })?
                .0
        }
        Value::Integer(number) => number.to_string(),
        Value::Real(number) if number.is_nan() => {
            return Err(raw(
                "builtins.ValueError",
                "cannot convert float NaN to integer",
            ));
        }
        Value::Real(number) if number.is_infinite() => {
            return Err(raw(
                "builtins.OverflowError",
                "cannot convert float infinity to integer",
            ));
        }
        Value::Real(number) if number.trunc() == 0.0 => "0".into(),
        Value::Real(number) => format!("{:.0}", number.trunc()),
        Value::Blob(bytes) => {
            let text = std::str::from_utf8(bytes)
                .ok()
                .filter(|text| text.is_ascii());
            match text.map(|text| integer(text, STAGE)) {
                Some(Ok(value)) => value.0,
                other => {
                    if let Some(Err(error)) = other
                        && error.message.starts_with("Exceeds the limit")
                    {
                        return Err(error);
                    }
                    return Err(raw_owned(
                        "builtins.ValueError",
                        format!(
                            "invalid literal for int() with base 10: {}",
                            bytes_repr(bytes).chars().take(200).collect::<String>()
                        ),
                    ));
                }
            }
        }
        Value::Null => {
            return Err(raw(
                "builtins.TypeError",
                "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'",
            ));
        }
    };
    Ok(Funded {
        value,
        _capacity: capacity,
    })
}

fn increment(value: &mut String) {
    // The integer conversion reservation includes the one possible new digit.
    let negative = value.starts_with('-');
    // SAFETY: Only existing ASCII decimal digits are changed in place.
    let digits = unsafe { value.as_bytes_mut() };
    for digit in digits.iter_mut().rev().take_while(|digit| **digit != b'-') {
        if negative {
            if *digit != b'0' {
                *digit -= 1;
                break;
            }
            *digit = b'9';
        } else if *digit != b'9' {
            *digit += 1;
            return;
        } else {
            *digit = b'0';
        }
    }
    if negative {
        if value.as_bytes()[1] == b'0' {
            value.remove(1);
            if value.len() == 1 {
                value.clear();
                value.push('0');
            }
        }
    } else {
        value.insert(0, '1');
    }
}

fn integer_equals(integer: &str, value: &Value) -> bool {
    match value {
        Value::Integer(number) => integer.parse::<i64>() == Ok(*number),
        Value::Real(number) if number.is_finite() && number.fract() == 0.0 => {
            let number = if *number == 0.0 {
                "0".into()
            } else {
                format!("{number:.0}")
            };
            integer == number
        }
        _ => false,
    }
}

fn canonical(value: Value) -> Result<CanonValue> {
    Ok(match value {
        Value::Null => CanonValue::Null,
        Value::Integer(number) => CanonValue::Int(number),
        Value::Real(number) if number.is_finite() => CanonValue::Float(number),
        Value::Real(_) => {
            return Err(raw(
                "builtins.ValueError",
                "Out of range float values are not JSON compliant",
            ));
        }
        Value::Text(text) => CanonValue::Str(text),
        Value::Blob(_) => {
            return Err(raw(
                "builtins.TypeError",
                "Object of type bytes is not JSON serializable",
            ));
        }
    })
}

fn binding_bytes(value: &Value) -> usize {
    match value {
        Value::Text(value) => value.len(),
        Value::Blob(value) => value.len(),
        _ => 0,
    }
}

fn python_id<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    value: &Value,
) -> Result<Funded<String>> {
    let capacity = op.reserve(
        STAGE,
        match value {
            Value::Text(text) => text.len(),
            Value::Blob(bytes) => bytes.len().saturating_mul(4).saturating_add(3),
            _ => 1024,
        },
    )?;
    let value = match value {
        Value::Text(text) => text.clone(),
        Value::Integer(value) => value.to_string(),
        Value::Null => "None".into(),
        Value::Blob(value) => bytes_repr(value),
        Value::Real(value) if value.is_nan() => "nan".into(),
        Value::Real(value) if value.is_infinite() => {
            if value.is_sign_negative() {
                "-inf".into()
            } else {
                "inf".into()
            }
        }
        Value::Real(value) => String::from_utf8(
            tobkiri_canon::encode(
                &CanonValue::Float(*value),
                tobkiri_canon::Variant::LegacyFloat,
            )
            .map_err(|_| OriginalAuthorityError::capacity(STAGE))?,
        )
        .expect("finite float encoding is UTF-8"),
    };
    Ok(Funded {
        value,
        _capacity: capacity,
    })
}

fn bytes_repr(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut text = String::with_capacity(bytes.len().saturating_mul(4).saturating_add(3));
    text.push('b');
    text.push(quote);
    for &byte in bytes {
        match byte {
            b'\\' => text.push_str("\\\\"),
            b'\n' => text.push_str("\\n"),
            b'\r' => text.push_str("\\r"),
            b'\t' => text.push_str("\\t"),
            byte if char::from(byte) == quote => {
                text.push('\\');
                text.push(quote);
            }
            0x20..=0x7e => text.push(char::from(byte)),
            byte => {
                let _ = write!(text, "\\x{byte:02x}");
            }
        }
    }
    text.push(quote);
    text
}

fn integer_repr(value: &str) -> String {
    use std::fmt::Write;
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut output =
        String::with_capacity(value.len().saturating_mul(10).saturating_add(2).min(810));
    output.push(quote);
    let mut characters = 1;
    for ch in value.chars() {
        if characters >= 200 {
            break;
        }
        let before = output.len();
        match ch {
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            ch if ch == quote => {
                output.push('\\');
                output.push(ch);
            }
            ch => {
                let point = u32::from(ch);
                let ranges = crate::unicode15_1_data::PRINTABLE_RANGES;
                let index = ranges.partition_point(|&(first, _)| first <= point);
                let printable = (0x20..0x7f).contains(&point)
                    || (point >= 0xa0 && index > 0 && point <= ranges[index - 1].1);
                if printable {
                    output.push(ch);
                    characters += 1;
                    continue;
                }
                if point < 0x100 {
                    let _ = write!(output, "\\x{point:02x}");
                } else if point < 0x10000 {
                    let _ = write!(output, "\\u{point:04x}");
                } else {
                    let _ = write!(output, "\\U{point:08x}");
                }
            }
        }
        characters += output.len() - before;
    }
    output.push(quote);
    if let Some((boundary, _)) = output.char_indices().nth(200) {
        output.truncate(boundary);
    }
    output
}

fn missing_column() -> OriginalAuthorityError {
    raw("builtins.IndexError", "No item with that key")
}
fn denied(message: &'static str) -> OriginalAuthorityError {
    raw("core_runtime.authority.v4_models.AuthorityDenied", message)
}
fn raw(class: &'static str, message: &'static str) -> OriginalAuthorityError {
    raw_owned(class, message.into())
}
fn raw_owned(class: &'static str, message: String) -> OriginalAuthorityError {
    let mut error = json::raw_error(class, message);
    error.stage = STAGE;
    error
}

#[cfg(test)]
#[path = "original_activation_authority_tests.rs"]
mod tests;
