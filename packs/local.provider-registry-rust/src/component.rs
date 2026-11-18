//! Wasm boundary for Registry operations under Core authority.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:provider-registry/pack@1.0.0",
    generate_all,
});

use crate::{
    component_store::SqlDocumentStore,
    failure::{SourceError, SourceResult},
    host::{MANAGE_OPERATION, READONLY_RESOURCE_OPERATION, invoke_manage, invoke_readonly},
    registry::Registry,
    state::Clock,
};
use ordered_python_compat::{CodecError, PyValue, dumps, loads};
use tobkiri::core::{
    clock, flow,
    types::{Error, ErrorCode},
};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl exports::local::provider_registry::resource::Guest for Pack {
    fn snapshot(payload: String) -> Result<String, Error> {
        let BoundRequest {
            mut registry,
            profile,
            payload,
        } = BoundRequest::open(&payload)?;
        let reply = invoke_readonly(
            &mut registry,
            &profile,
            READONLY_RESOURCE_OPERATION,
            &payload,
        )
        .map_err(source_error)?;
        encode_reply(&reply)
    }
}

impl exports::local::provider_registry::manage::Guest for Pack {
    fn apply(payload: String) -> Result<String, Error> {
        let BoundRequest {
            mut registry,
            profile,
            payload,
        } = BoundRequest::open(&payload)?;
        let reply = invoke_manage(&mut registry, &profile, MANAGE_OPERATION, &payload)
            .map_err(source_error)?;
        // The pure Registry has staged its update. A reply outside Core's JSON
        // domain must fail before the database compare-and-swap.
        let reply = encode_reply(&reply)?;
        registry.store_mut().commit().map_err(source_error)?;
        Ok(reply)
    }
}

export!(Pack);

struct BoundRequest {
    registry: Registry<SqlDocumentStore, CoreClock>,
    profile: String,
    payload: PyValue,
}

impl BoundRequest {
    fn open(payload: &str) -> Result<Self, Error> {
        let profile = flow::current_profile().map_err(|error| {
            boundary_error(error.code, "provider registry profile is unavailable")
        })?;
        let payload = loads(payload).map_err(codec_error)?;
        let store = SqlDocumentStore::new();
        let registry =
            Registry::new(&PyValue::String(profile.clone()), store, CoreClock).map_err(|_| {
                boundary_error(ErrorCode::Denied, "provider registry profile is invalid")
            })?;
        Ok(Self {
            registry,
            profile,
            payload,
        })
    }
}

fn encode_reply(value: &PyValue) -> Result<String, Error> {
    let encoded = dumps(value).map_err(codec_error)?;
    tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).map_err(
        |error| match error {
            tobkiri_canon::Error::Limit(_) => boundary_error(
                ErrorCode::LimitExceeded,
                "provider registry reply exceeds Core JSON limits",
            ),
            _ => boundary_error(
                ErrorCode::Invalid,
                "provider registry reply is outside the Core JSON domain",
            ),
        },
    )?;
    Ok(encoded)
}

fn codec_error(error: CodecError) -> Error {
    match error {
        CodecError::Invalid | CodecError::NonFinite => {
            boundary_error(ErrorCode::Invalid, "provider registry JSON is invalid")
        }
        _ => boundary_error(
            ErrorCode::LimitExceeded,
            "provider registry JSON exceeds limits",
        ),
    }
}

fn source_error(error: SourceError) -> Error {
    let (code, message) = match error.exception_class {
        "ProviderRegistryConflict" => (ErrorCode::Conflict, "provider registry revision is stale"),
        "StorageConflict" => (ErrorCode::Conflict, "provider registry storage conflict"),
        "PermissionError" => (ErrorCode::Denied, "provider registry request is denied"),
        "ApprovalRequired" => (
            ErrorCode::ApprovalRequired,
            "provider registry approval is required",
        ),
        "StorageCancelled" => (
            ErrorCode::Cancelled,
            "provider registry storage was cancelled",
        ),
        "StorageLimitExceeded" => (
            ErrorCode::LimitExceeded,
            "provider registry storage exceeds limits",
        ),
        "StorageUnavailable" | "ClockUnavailable" => (
            ErrorCode::Unavailable,
            "provider registry operation is unavailable",
        ),
        "KeyError" if error.message == "'provider instance is unknown'" => {
            (ErrorCode::NotFound, "provider instance is unknown")
        }
        "ValueError" if error.message == "provider registry profile binding is invalid" => {
            (ErrorCode::Denied, "provider registry profile is invalid")
        }
        "ValueError" | "TypeError" | "AttributeError" | "OverflowError" => {
            (ErrorCode::Invalid, "provider registry data is invalid")
        }
        _ => (ErrorCode::Internal, "provider registry operation failed"),
    };
    boundary_error(code, message)
}

fn boundary_error(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}

struct CoreClock;

impl Clock for CoreClock {
    fn now(&mut self) -> SourceResult<String> {
        utc_timestamp(clock::now_ms())
    }
}

fn utc_timestamp(ms: u64) -> SourceResult<String> {
    const SECONDS_PER_DAY: u64 = 86_400;
    const DAYS_PER_400_YEARS: u64 = 146_097;
    let seconds = ms / 1_000;
    // March-based Gregorian eras put each leap day at the end of its year.
    let days = seconds / SECONDS_PER_DAY + 719_468;
    let era = days / DAYS_PER_400_YEARS;
    let day_of_era = days % DAYS_PER_400_YEARS;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let march_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * march_month + 2) / 5 + 1;
    let month = (march_month + 2) % 12 + 1;
    let year = era * 400 + year_of_era + u64::from(month <= 2);
    if year > 9_999 {
        return Err(SourceError::new(
            "ClockUnavailable",
            "clock date is out of range",
        ));
    }
    let hour = seconds % SECONDS_PER_DAY / 3_600;
    let minute = seconds % 3_600 / 60;
    let second = seconds % 60;
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn core_milliseconds_become_original_utc_seconds() {
        for (ms, expected) in [
            (0, "1970-01-01T00:00:00Z"),
            (999, "1970-01-01T00:00:00Z"),
            (951_827_696_789, "2000-02-29T12:34:56Z"),
            (4_107_542_400_000, "2100-03-01T00:00:00Z"),
            (253_402_300_799_999, "9999-12-31T23:59:59Z"),
        ] {
            assert_eq!(utc_timestamp(ms).unwrap(), expected);
        }
        assert_eq!(
            utc_timestamp(u64::MAX).unwrap_err().exception_class,
            "ClockUnavailable"
        );
    }

    #[test]
    fn reply_encoder_enforces_core_numeric_and_depth_limits() {
        assert_eq!(
            encode_reply(&PyValue::Int("9007199254740991".into())).unwrap(),
            "9007199254740991"
        );
        for value in [PyValue::Int("9007199254740992".into()), PyValue::Float(1.0)] {
            assert!(matches!(
                encode_reply(&value).unwrap_err().code,
                ErrorCode::Invalid
            ));
        }
        let mut value = PyValue::Null;
        for _ in 0..=tobkiri_canon::MAX_DEPTH {
            value = PyValue::Array(vec![value]);
        }
        assert!(matches!(
            encode_reply(&value).unwrap_err().code,
            ErrorCode::LimitExceeded
        ));
    }

    #[test]
    fn source_errors_keep_boundary_codes_without_exposing_details() {
        for (error, expected) in [
            (SourceError::permission("private input"), ErrorCode::Denied),
            (SourceError::conflict("private input"), ErrorCode::Conflict),
            (SourceError::value("private input"), ErrorCode::Invalid),
            (
                SourceError::new("StorageError", "private input"),
                ErrorCode::Internal,
            ),
        ] {
            let error = source_error(error);
            assert_eq!(error.code as u8, expected as u8);
            assert!(!error.message.contains("private input"));
        }
    }
}
