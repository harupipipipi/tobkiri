//! Stage the original file bytes in the isolated native execution Store.

use super::tobkiri::core::{
    flow,
    types::{CallbackHandle, Error, ErrorCode},
};
use super::{
    LiveObservedRegistry, Pack, boundary_error, codec_error, core_json, current_profile,
    exports::local::provider_registry::{
        configuration::LocalConnection, legacy_configuration_apply,
    },
    local_request, source_error,
};
use crate::{
    configuration::stage_legacy_local_configuration,
    failure::{SourceError, SourceResult},
    registry::Registry,
    state::{Clock, DocumentStore, object, text},
};
use ordered_python_compat::{
    CodecError, PyValue, dumps_bounded, dumps_pretty_document, loads, utc_timestamp_from_seconds,
};

// The installed Provider artifact allows 1 MiB of output, including escaping
// and the result envelope. The raw document cannot exceed that bound either.
const MAX_STAGE_REPLY_BYTES: usize = 1024 * 1024;

impl legacy_configuration_apply::Guest for Pack {
    fn stage_local(
        request: LocalConnection,
        plan: String,
        observe: CallbackHandle,
    ) -> Result<String, Error> {
        let plan = loads(&plan).map_err(codec_error)?;
        let store = StagedRegistry {
            observed: LiveObservedRegistry::new(observe),
            document: None,
        };
        let clock = ObservationClock {
            observe,
            callback_error: None,
        };
        let mut registry =
            Registry::new(&text(current_profile()?), store, clock).map_err(source_error)?;
        let result =
            stage_legacy_local_configuration(&mut registry, &local_request(request), &plan);
        let (store, clock) = registry.into_parts();
        let result = result.map_err(|error| match clock.callback_error {
            Some(code) => boundary_error(code, "provider registry clock failed"),
            None => store.observed.map_error(error),
        })?;
        let document = store
            .document
            .expect("a successful save stages the document");
        encode_stage_reply(document, result)
    }
}

struct StagedRegistry {
    observed: LiveObservedRegistry,
    document: Option<String>,
}

impl DocumentStore for StagedRegistry {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        self.observed.read()
    }

    fn write(&mut self, document: &PyValue) -> SourceResult<()> {
        let encoded =
            dumps_pretty_document(document, MAX_STAGE_REPLY_BYTES).map_err(
                |error| match error {
                    CodecError::Invalid | CodecError::NonFinite => {
                        SourceError::value("provider registry document is invalid")
                    }
                    _ => SourceError::new(
                        "StorageLimitExceeded",
                        "provider registry document is too large",
                    ),
                },
            )?;
        self.document = Some(encoded);
        Ok(())
    }
}

fn encode_stage_reply(document: String, result: PyValue) -> Result<String, Error> {
    let value = object([("document", text(document)), ("result", result)]);
    let encoded = dumps_bounded(&value, MAX_STAGE_REPLY_BYTES).map_err(codec_error)?;
    core_json(&encoded)?;
    Ok(encoded)
}

struct ObservationClock {
    observe: CallbackHandle,
    callback_error: Option<ErrorCode>,
}

impl Clock for ObservationClock {
    fn now(&mut self) -> SourceResult<String> {
        let reply = flow::invoke_callback(self.observe, r#""clock""#).map_err(|error| {
            self.callback_error = Some(error.code);
            SourceError::new("ClockUnavailable", "provider registry clock failed")
        })?;
        decode_clock_reply(&reply)
    }
}

fn decode_clock_reply(reply: &str) -> SourceResult<String> {
    let invalid = || SourceError::value("provider registry clock reply is invalid");
    if reply.is_empty()
        || reply.len() > 20
        || (reply.len() > 1 && reply.starts_with('0'))
        || !reply.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(invalid());
    }
    let seconds = reply.parse().map_err(|_| invalid())?;
    utc_timestamp_from_seconds(seconds)
        .ok_or_else(|| SourceError::new("ClockUnavailable", "clock date is out of range"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clock_requires_bare_canonical_unsigned_seconds() {
        assert_eq!(decode_clock_reply("0").unwrap(), "1970-01-01T00:00:00Z");
        assert_eq!(
            decode_clock_reply("951827696").unwrap(),
            "2000-02-29T12:34:56Z"
        );
        for malformed in [
            "",
            " 1",
            "1\n",
            "01",
            "+1",
            "-1",
            "1.0",
            "1e0",
            "\"1\"",
            "18446744073709551616",
            "９",
        ] {
            assert_eq!(
                decode_clock_reply(malformed).unwrap_err().exception_class,
                "ValueError"
            );
        }
        assert_eq!(
            decode_clock_reply("253402300800")
                .unwrap_err()
                .exception_class,
            "ClockUnavailable"
        );
    }

    #[test]
    fn envelope_limit_accounts_for_escaped_document_bytes() {
        let document = "\n".repeat(MAX_STAGE_REPLY_BYTES / 2);
        let result = object([
            ("configured", PyValue::Bool(true)),
            ("provider_instance_id", text("provider.fixture")),
        ]);
        assert!(matches!(
            encode_stage_reply(document, result).unwrap_err().code,
            ErrorCode::LimitExceeded
        ));
    }
}
