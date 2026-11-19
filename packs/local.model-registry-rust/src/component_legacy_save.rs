//! Stage a replacement for the original Model Registry file.

use super::tobkiri::core::{
    flow,
    types::{CallbackHandle, Error, ErrorCode},
};
use super::{
    MAX_LEGACY_DOCUMENT_BYTES, Pack, codec_error, exports::local::model_registry::legacy_save,
    failure, legacy_codec_required, provider_binding, source_error, validate_provider_snapshot,
};
use crate::{
    SourceError,
    mutation::assert_revision,
    registry::{Registry, object},
    settings_save::{captured_save_result, normalize, present_saved},
};
use ordered_python_compat::{
    CodecError, PyValue, dumps_bounded, dumps_pretty_document, loads, loads_python_json,
    utc_timestamp_from_seconds,
};

// The output ceiling includes the escaped replacement, owner result and public projection.
const MAX_STAGE_REPLY_BYTES: usize = 1024 * 1024;

impl legacy_save::Guest for Pack {
    fn stage_model(
        request: String,
        provider: String,
        observe: CallbackHandle,
    ) -> Result<String, Error> {
        let profile = flow::current_profile()
            .map_err(|error| observation_error(error, "model Registry Profile lookup failed"))?;
        stage_model(&profile, &request, &provider, |args| {
            flow::invoke_callback(observe, args)
        })
    }
}

impl From<SourceError> for Error {
    fn from(error: SourceError) -> Self {
        source_error(error)
    }
}

fn stage_model(
    profile: &str,
    request: &str,
    provider: &str,
    mut observe: impl FnMut(&str) -> Result<String, Error>,
) -> Result<String, Error> {
    let request = normalize(&loads(request).map_err(stage_codec_error)?)?;
    let provider = loads(provider).map_err(stage_codec_error)?;
    validate_provider_snapshot(provider_binding(&request)?, &provider)?;

    let mut registry = Registry::new(profile, None)?;
    let reply =
        observe("null").map_err(|error| observation_error(error, "model Registry read failed"))?;
    if let Some(document) = decode_observation(&registry, &request, &reply)? {
        registry.document = document;
    }
    let (replacement, saved) = registry.apply_settings_with_clock(&request, || {
        let reply = observe(r#""clock""#)
            .map_err(|error| observation_error(error, "model Registry clock failed"))?;
        decode_clock(&reply)
    })?;
    let captured = captured_save_result(&request, &saved)?;
    let result = present_saved(&captured)?;
    encode_reply(replacement.as_ref(), result, &captured)
}

fn decode_observation(
    registry: &Registry,
    request: &PyValue,
    reply: &str,
) -> Result<Option<PyValue>, Error> {
    // The JSON string can escape a raw byte as six ASCII bytes, plus quotes.
    if reply.len() > 6 * MAX_LEGACY_DOCUMENT_BYTES + 2 {
        return Err(failure(
            ErrorCode::LimitExceeded,
            "model Registry observation exceeds limits",
        ));
    }
    let raw = serde_json::from_str::<Option<String>>(reply).map_err(|_| {
        failure(
            ErrorCode::Internal,
            "model Registry observation reply is invalid",
        )
    })?;
    let Some(raw) = raw else {
        return Ok(None);
    };
    if raw.len() > MAX_LEGACY_DOCUMENT_BYTES {
        return Err(failure(
            ErrorCode::LimitExceeded,
            "model Registry document exceeds limits",
        ));
    }
    match loads(&raw) {
        Ok(document) => Ok(Some(document)),
        Err(error @ (CodecError::Invalid | CodecError::NonFinite | CodecError::NodeLimit)) => {
            let document = loads_python_json(&raw).map_err(stage_codec_error)?;
            registry.validate_legacy_document(&document)?;
            let revision = document
                .decode_field("revision")
                .map_err(stage_codec_error)?;
            assert_revision(revision.as_ref(), request)?;
            Err(if error == CodecError::NodeLimit {
                stage_codec_error(error)
            } else {
                legacy_codec_required()
            })
        }
        Err(error) => Err(stage_codec_error(error)),
    }
}

fn stage_codec_error(error: CodecError) -> Error {
    match error {
        // The integer digit limit applies to Python's saved format as well.
        CodecError::IntegerLimit => codec_error(CodecError::Invalid),
        CodecError::Resource => {
            failure(ErrorCode::Internal, "model Registry JSON allocation failed")
        }
        _ => codec_error(error),
    }
}

fn observation_error(error: Error, message: &str) -> Error {
    // Unavailable is reserved for an identified Python JSON compatibility case.
    let code = match error.code {
        ErrorCode::Denied | ErrorCode::Cancelled => error.code,
        _ => ErrorCode::Internal,
    };
    failure(code, message)
}

fn decode_clock(reply: &str) -> Result<String, Error> {
    let invalid = || failure(ErrorCode::Internal, "model Registry clock reply is invalid");
    if reply.is_empty()
        || reply.len() > 20
        || (reply.len() > 1 && reply.starts_with('0'))
        || !reply.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(invalid());
    }
    let seconds = reply.parse().map_err(|_| invalid())?;
    utc_timestamp_from_seconds(seconds).ok_or_else(invalid)
}

fn encode_reply(
    replacement: Option<&PyValue>,
    result: PyValue,
    owner_result: &PyValue,
) -> Result<String, Error> {
    let document = replacement
        .map(|document| dumps_pretty_document(document, MAX_STAGE_REPLY_BYTES))
        .transpose()
        .map_err(stage_codec_error)?
        .map_or(PyValue::Null, PyValue::String);
    // The presenter returns only scalar fields; the revision is its only
    // integer that may be outside Core's safe range.
    let wide_revision = matches!(result.get("registry_revision"), Some(PyValue::Int(digits))
        if !digits.parse::<i64>().is_ok_and(|value| value.unsigned_abs() <= tobkiri_canon::MAX_SAFE_INTEGER));
    let owner_result =
        dumps_bounded(owner_result, MAX_STAGE_REPLY_BYTES).map_err(stage_codec_error)?;
    let reply = object([
        ("document", document),
        ("result", result),
        ("owner_result", PyValue::String(owner_result)),
    ]);
    let encoded = dumps_bounded(&reply, MAX_STAGE_REPLY_BYTES).map_err(stage_codec_error)?;
    if wide_revision {
        return Err(legacy_codec_required());
    }
    tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).map_err(|_| {
        failure(
            ErrorCode::Internal,
            "model Registry stage response is invalid",
        )
    })?;
    Ok(encoded)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::dumps;
    use sha2::{Digest, Sha256};

    const FIXTURE: &str = include_str!("../evidence/original-python-save.json");
    const PROVIDER: &str = r#"{"revision":7,"providers":[{"provider_instance_id":"example-provider","enabled":true}]}"#;
    const CLOCK: &str = "1792586096";

    fn fixture() -> PyValue {
        loads(FIXTURE).unwrap()
    }

    fn case(fixture: &PyValue, index: usize) -> &PyValue {
        &fixture.get("cases").unwrap().as_array().unwrap()[index]
    }

    fn invoke(
        case: &PyValue,
        provider: &str,
        observe: impl FnMut(&str) -> Result<String, Error>,
    ) -> Result<String, Error> {
        stage_model(
            case.get("profile_id").unwrap().as_str().unwrap(),
            &dumps(case.get("request").unwrap()).unwrap(),
            provider,
            observe,
        )
    }

    #[test]
    fn stages_pinned_python_saves_and_omits_no_op_writes_and_clocks() {
        let fixture = fixture();
        let cases = fixture.get("cases").unwrap().as_array().unwrap();
        assert_eq!(cases.len(), 7);
        for case in cases {
            let id = case.get("id").unwrap().as_str().unwrap();
            let original = case.get("document").unwrap();
            let raw = (!matches!(original, PyValue::Null)).then(|| dumps(original).unwrap());
            let mut observed = Vec::new();
            let reply = invoke(case, PROVIDER, |args| {
                observed.push(args.to_owned());
                Ok(match args {
                    "null" => serde_json::to_string(&raw).unwrap(),
                    r#""clock""# => CLOCK.into(),
                    _ => panic!("unexpected observation {args}"),
                })
            });
            if let Some(original_result) = case.get("result") {
                let reply =
                    loads(&reply.unwrap_or_else(|error| panic!("{id}: {error:?}"))).unwrap();
                let expected = case.get("presented").cloned().unwrap_or_else(|| {
                    assert_eq!(id, "create-existing-route");
                    // The original host strips the existing output cap before presentation.
                    let created = cases[0].get("presented").unwrap();
                    object([
                        ("profiles", created.get("profiles").unwrap().clone()),
                        ("count", created.get("count").unwrap().clone()),
                        (
                            "registry_revision",
                            original_result.get("store_revision").unwrap().clone(),
                        ),
                    ])
                });
                assert_eq!(
                    dumps(reply.get("result").unwrap()).unwrap(),
                    dumps(&expected).unwrap(),
                    "{id}"
                );
                let owner_result = reply.get("owner_result").unwrap().as_str().unwrap();
                if id.starts_with("create-") {
                    let original_profile = original_result.get("profile").unwrap();
                    let owner_profile = object([
                        (
                            "model_profile_id",
                            original_profile.get("model_profile_id").unwrap().clone(),
                        ),
                        (
                            "model_id",
                            original_profile.get("model_id").unwrap().clone(),
                        ),
                        (
                            "display_name",
                            original_profile.get("display_name").unwrap().clone(),
                        ),
                        ("enabled", original_profile.get("enabled").unwrap().clone()),
                        (
                            "metadata",
                            loads(r#"{"provider_connection_id":"example-provider"}"#).unwrap(),
                        ),
                    ]);
                    assert_eq!(
                        owner_result,
                        dumps(&object([
                            ("action", original_result.get("action").unwrap().clone()),
                            (
                                "store_revision",
                                original_result.get("store_revision").unwrap().clone()
                            ),
                            ("profile", owner_profile),
                        ]))
                        .unwrap(),
                        "{id}"
                    );
                } else {
                    assert_eq!(owner_result, dumps(original_result).unwrap(), "{id}");
                }
                let replacement = reply.get("document").unwrap();
                let expected = case.get("updated").unwrap();
                let digest = match id {
                    // SHA-256 of the pinned Python state encoded with its original file writer.
                    "create-first-route" => {
                        "83fbf73a10128cabd317d3afb029005fe1a9164c2eecf466c9262b2a13448798"
                    }
                    "save-updated-cap" => {
                        "b29a28396d12e6270be4008a363c8255b58521a2b3adcd122f66c9820fe3c3ba"
                    }
                    _ => {
                        assert_eq!(replacement, &PyValue::Null, "{id}");
                        assert_eq!(expected, &PyValue::Null, "{id}");
                        assert_eq!(observed, ["null"], "{id}");
                        continue;
                    }
                };
                let raw = replacement.as_str().unwrap();
                assert_eq!(format!("{:x}", Sha256::digest(raw)), digest, "{id}");
                assert_eq!(
                    dumps(&loads(raw).unwrap()).unwrap(),
                    dumps(expected).unwrap(),
                    "{id}"
                );
                assert_eq!(observed, ["null", r#""clock""#], "{id}");
            } else {
                let error = reply.expect_err(id);
                match id {
                    "create-stale-model-revision" => {
                        assert!(matches!(error.code, ErrorCode::Conflict))
                    }
                    _ => assert!(matches!(error.code, ErrorCode::Invalid)),
                }
                if id == "save-invalid-float-cap" {
                    assert!(observed.is_empty(), "{id}");
                } else {
                    assert_eq!(observed, ["null"], "{id}");
                }
            }
        }
    }

    #[test]
    fn rejects_stale_and_nonunique_enabled_provider_before_observing_model() {
        let fixture = fixture();
        let case = case(&fixture, 0);
        for snapshot in [
            r#"{"revision":6,"providers":[]}"#,
            r#"{"revision":7,"providers":[{"provider_instance_id":"foreign","enabled":true}]}"#,
            r#"{"revision":7,"providers":[{"provider_instance_id":"example-provider","enabled":false}]}"#,
            r#"{"revision":7,"providers":[{"provider_instance_id":"example-provider","enabled":true},{"provider_instance_id":"example-provider","enabled":true}]}"#,
        ] {
            let error = invoke(case, snapshot, |_| panic!("unexpected observation")).unwrap_err();
            assert!(matches!(error.code, ErrorCode::Denied));
        }
    }

    #[test]
    fn distinguishes_corrupt_documents_and_lost_observations_from_legacy_codec_cases() {
        let fixture = fixture();
        let case = case(&fixture, 0);
        for raw in ["null", "", "{", "[]", "NaN"] {
            let error = invoke(case, PROVIDER, |args| {
                assert_eq!(args, "null");
                Ok(serde_json::to_string(raw).unwrap())
            })
            .unwrap_err();
            assert!(matches!(error.code, ErrorCode::Invalid), "{raw}");
        }
        let foreign = r#"{"version":"rumi.model-registry.store.v1","profile_id":"foreign","revision":0,"profiles":{},"aliases":{}}"#;
        let error = invoke(case, PROVIDER, |_| {
            Ok(serde_json::to_string(foreign).unwrap())
        })
        .unwrap_err();
        assert!(matches!(error.code, ErrorCode::Denied));
        for (raw, expected) in [
            (
                r#"{"version":"rumi.model-registry.store.v1","profile_id":"prof_model_fixture","profile_id":"foreign","revision":0,"profiles":{},"aliases":{}}"#,
                ErrorCode::Denied,
            ),
            (
                r#"{"version":"rumi.model-registry.store.v1","profile_id":"prof_model_fixture","revision":1,"profiles":{},"aliases":{},"extra":NaN}"#,
                ErrorCode::Conflict,
            ),
            (
                r#"{"version":"rumi.model-registry.store.v1","profile_id":"prof_model_fixture","revision":0,"profiles":[],"aliases":{},"extra":NaN}"#,
                ErrorCode::Invalid,
            ),
            (
                r#"{"version":"rumi.model-registry.store.v1","profile_id":"prof_model_fixture","revision":NaN,"profiles":{},"aliases":{}}"#,
                ErrorCode::Invalid,
            ),
        ] {
            let error =
                invoke(case, PROVIDER, |_| Ok(serde_json::to_string(raw).unwrap())).unwrap_err();
            assert_eq!(
                std::mem::discriminant(&error.code),
                std::mem::discriminant(&expected)
            );
        }
        for opaque in [r#"{"key":1,"key":2}"#, "NaN", r#""\ud800""#] {
            let raw = format!(
                r#"{{"version":"rumi.model-registry.store.v1","profile_id":"prof_model_fixture","revision":0,"profiles":{{}},"aliases":{{}},"extra":{opaque}}}"#
            );
            let error = invoke(case, PROVIDER, |args| {
                assert_eq!(args, "null");
                Ok(serde_json::to_string(&raw).unwrap())
            })
            .unwrap_err();
            assert!(matches!(error.code, ErrorCode::Unavailable), "{raw}");
        }
        let error = invoke(case, PROVIDER, |_| {
            Err(failure(ErrorCode::Unavailable, "private data"))
        })
        .unwrap_err();
        assert!(matches!(error.code, ErrorCode::Internal));
        assert_eq!(error.message, "model Registry read failed");
    }

    #[test]
    fn stage_reply_limit_includes_document_escaping_and_envelope() {
        let provisional = PyValue::String("\\".repeat(MAX_STAGE_REPLY_BYTES / 4));
        assert!(dumps_pretty_document(&provisional, MAX_STAGE_REPLY_BYTES).is_ok());
        let error =
            encode_reply(Some(&provisional), loads("{}").unwrap(), &PyValue::Null).unwrap_err();
        assert!(matches!(error.code, ErrorCode::LimitExceeded));
        let error = encode_reply(None, loads("{}").unwrap(), &provisional).unwrap_err();
        assert!(matches!(error.code, ErrorCode::LimitExceeded));
    }

    #[test]
    fn recognizes_wide_projected_revision_and_malformed_clock() {
        let result =
            loads(r#"{"profiles":[],"count":0,"registry_revision":9007199254740992}"#).unwrap();
        let error = encode_reply(None, result, &PyValue::Null).unwrap_err();
        assert!(matches!(error.code, ErrorCode::Unavailable));
        assert_eq!(decode_clock("0").unwrap(), "1970-01-01T00:00:00Z");
        for malformed in ["", " 1", "01", "1.0", r#""1""#, "-1", "253402300800"] {
            assert!(matches!(
                decode_clock(malformed).unwrap_err().code,
                ErrorCode::Internal
            ));
        }
    }
}
