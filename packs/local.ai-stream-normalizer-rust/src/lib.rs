//! Additive Rust translation of the original pure stream normalizer.
//!
//! The generated WIT bindings reuse the original Pack contract unchanged. This
//! implementation identifies itself as Rust and makes no CPython runtime claim.

wit_bindgen::generate!({
    path: [
        "../../crates/tobkiri-wit/wit/core/core.wit",
        "../local.ai-stream-normalizer-python/wit",
    ],
    world: "local:ai-stream-normalizer-python/pack@1.0.0",
    generate_all,
});

use exports::local::ai_stream_normalizer_python::stream_normalizer::Guest as StreamNormalizer;
use exports::tobkiri::core::lifecycle;
use ordered_python_compat::{
    CodecError, CoercionError, MAX_BYTES, MAX_NODES, PyValue, dumps, loads, pyint, pystr, pystrip,
    pytruthy,
};
use tobkiri::core::types::{Error, ErrorCode};

const PACK_ID: &str = "local.ai-stream-normalizer-rust";
const ALLOWED_TYPES: [&str; 6] = [
    "text_delta",
    "thinking_delta",
    "tool_intent_delta",
    "usage",
    "finish",
    "error",
];

const OPERATIONS: [&str; 3] = [
    "normalize",
    "validate",
    "rumi_ai_stream_pack.ai-stream-normalize",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Failure {
    Input,
    Validation,
    Arithmetic,
    Output,
    Limit,
}

impl Failure {
    fn into_wit_error(self) -> Error {
        let (code, message) = match self {
            Self::Input => (ErrorCode::Invalid, "invalid stream normalizer request"),
            Self::Validation => (
                ErrorCode::Internal,
                "stream normalizer provider validation failed",
            ),
            Self::Arithmetic => (
                ErrorCode::Internal,
                "stream normalizer provider arithmetic failed",
            ),
            Self::Output => (
                ErrorCode::Internal,
                "stream normalizer provider output failed",
            ),
            Self::Limit => (
                ErrorCode::LimitExceeded,
                "stream normalizer resource limit exceeded",
            ),
        };
        Error {
            code,
            message: message.into(),
        }
    }
}

struct Pack;

fn runtime_info_data() -> String {
    format!(
        r#"{{"implementation":"rust","pack_id":"{PACK_ID}","protocol":"ordered-finite-json-v2"}}"#
    )
}

impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl StreamNormalizer for Pack {
    fn runtime_info() -> String {
        // This identity deliberately describes the Rust candidate, not CPython.
        runtime_info_data()
    }

    fn normalize(operation: String, payload: String) -> Result<String, Error> {
        normalize_data(&operation, &payload).map_err(Failure::into_wit_error)
    }
}

fn normalize_data(operation: &str, payload: &str) -> Result<String, Failure> {
    if !OPERATIONS.contains(&operation) {
        return Err(Failure::Input);
    }

    let parsed = loads(payload).map_err(map_input_error)?;
    let mut request = parsed.into_object().ok_or(Failure::Input)?;

    let request_id_value = take_field(&mut request, "request_id");
    let request_id = match request_id_value.as_ref().filter(|value| pytruthy(value)) {
        Some(value) => pystr(value),
        None => String::new(),
    };
    let request_id = pystrip(&request_id).to_owned();

    let attempt_value = take_field(&mut request, "provider_attempt");
    let attempt_value = match attempt_value.filter(pytruthy) {
        Some(value) => value,
        None => PyValue::Int("1".into()),
    };
    let provider_attempt = pyint(&attempt_value).map_err(map_int_error)?;
    if request_id.is_empty() {
        return Err(Failure::Validation);
    }

    let value = take_field(&mut request, "value").unwrap_or(PyValue::Null);
    let events = if matches!(&value, PyValue::Array(_)) {
        value.into_array().ok_or(Failure::Validation)?
    } else if matches!(&value, PyValue::Object(_)) {
        let mut fields = value.into_object().ok_or(Failure::Validation)?;
        take_field(&mut fields, "events")
            .and_then(PyValue::into_array)
            .ok_or(Failure::Validation)?
    } else {
        return Err(Failure::Validation);
    };

    // The output always has a three-node envelope plus 19 nodes per projected
    // event. Also bound repeated copies of caller-controlled request_id before
    // materializing a larger tree. We still validate every event before
    // returning an output-limit error, matching the Python adapter's order.
    let node_limit_exceeded = events.len() > (MAX_NODES - 3) / 19;
    let repeated_scalar_bytes = request_id
        .len()
        .checked_add(provider_attempt.len())
        .and_then(|bytes| bytes.checked_mul(events.len()));
    let output_limit_exceeded =
        node_limit_exceeded || repeated_scalar_bytes.is_none_or(|bytes| bytes > MAX_BYTES);

    let mut normalized = Vec::new();
    if !output_limit_exceeded {
        normalized
            .try_reserve_exact(events.len())
            .map_err(|_| Failure::Limit)?;
    }
    let event_count = events.len();
    let mut finished = false;

    for (sequence, event) in events.into_iter().enumerate() {
        let mut fields = event.into_object().ok_or(Failure::Validation)?;

        let type_value = take_field(&mut fields, "type").unwrap_or(PyValue::Null);
        let type_value = if pytruthy(&type_value) {
            type_value
        } else {
            PyValue::String(String::new())
        };
        let event_type = pystr(&type_value);
        if !ALLOWED_TYPES.contains(&event_type.as_str()) || finished {
            return Err(Failure::Validation);
        }

        if !output_limit_exceeded {
            let mut projected = Vec::new();
            projected.try_reserve_exact(9).map_err(|_| Failure::Limit)?;
            projected.push(("request_id".into(), PyValue::String(request_id.clone())));
            projected.push(("sequence".into(), PyValue::Int(sequence.to_string())));
            projected.push(("type".into(), PyValue::String(event_type.clone())));
            projected.push((
                "delta".into(),
                take_field(&mut fields, "delta").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "tool_intent".into(),
                take_field(&mut fields, "tool_intent").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "usage".into(),
                take_field(&mut fields, "usage").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "finish_reason".into(),
                take_field(&mut fields, "finish_reason").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "error_code".into(),
                take_field(&mut fields, "error_code").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "provider_attempt".into(),
                PyValue::Int(provider_attempt.clone()),
            ));
            normalized.push(PyValue::Object(projected));
        }
        finished = event_type == "finish" || event_type == "error";
    }

    if event_count == 0 || !finished {
        return Err(Failure::Validation);
    }
    if output_limit_exceeded {
        return Err(Failure::Output);
    }

    dumps(&PyValue::object([(
        "events".into(),
        PyValue::Array(normalized),
    )]))
    .map_err(map_output_error)
}

fn take_field(fields: &mut Vec<(String, PyValue)>, name: &str) -> Option<PyValue> {
    let index = fields.iter().position(|(key, _)| key == name)?;
    Some(fields.swap_remove(index).1)
}

fn map_int_error(error: CoercionError) -> Failure {
    match error {
        CoercionError::Overflow => Failure::Arithmetic,
        CoercionError::Type | CoercionError::Value => Failure::Validation,
    }
}

fn map_input_error(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Limit,
        _ => Failure::Input,
    }
}

fn map_output_error(error: CodecError) -> Failure {
    match error {
        CodecError::NonFinite => Failure::Arithmetic,
        CodecError::Resource => Failure::Limit,
        CodecError::Invalid
        | CodecError::ByteLimit
        | CodecError::NodeLimit
        | CodecError::IntegerLimit => Failure::Output,
    }
}

#[cfg(target_arch = "wasm32")]
export!(Pack);

#[cfg(test)]
mod tests {
    use super::{
        CodecError, ErrorCode, Failure, dumps, loads, map_input_error, map_output_error,
        normalize_data, runtime_info_data,
    };
    use ordered_python_compat::{MAX_BYTES, PyValue};
    use sha2::{Digest as _, Sha256};
    use std::{
        collections::BTreeSet,
        fs,
        io::Write as _,
        path::{Component, Path, PathBuf},
    };

    const MAX_RECEIPT_BYTES: u64 = 1024 * 1024;
    const MAX_SOURCE_BYTES: u64 = 8 * 1024 * 1024;
    const MAX_CASE_INPUT_BYTES: u64 = 4 * 1024 * 1024 + 128;
    const MAX_CASE_OUTPUT_BYTES: u64 = 1024 * 1024;
    const MAX_CASES: usize = 128;
    const MAX_SOURCES: usize = 64;

    fn field<'a>(value: &'a PyValue, name: &str) -> &'a PyValue {
        value
            .get(name)
            .unwrap_or_else(|| panic!("missing receipt field {name}"))
    }

    fn string<'a>(value: &'a PyValue, context: &str) -> &'a str {
        value
            .as_str()
            .unwrap_or_else(|| panic!("{context} must be a string"))
    }

    fn size(value: &PyValue, context: &str) -> u64 {
        match value {
            PyValue::Int(value) => value
                .parse()
                .unwrap_or_else(|_| panic!("{context} must be a bounded integer")),
            _ => panic!("{context} must be an integer"),
        }
    }

    fn sha256_hex(bytes: &[u8]) -> String {
        format!("{:x}", Sha256::digest(bytes))
    }

    fn read_bounded_file(path: &Path, limit: u64) -> Vec<u8> {
        let metadata = fs::symlink_metadata(path).expect("oracle sidecar must exist");
        assert!(
            metadata.file_type().is_file(),
            "oracle sidecar must be a regular file"
        );
        assert!(
            metadata.len() <= limit,
            "oracle sidecar exceeds its byte bound"
        );
        let bytes = fs::read(path).expect("oracle sidecar must be readable");
        assert_eq!(
            metadata.len(),
            bytes.len() as u64,
            "oracle sidecar changed size"
        );
        bytes
    }

    fn new_actual_directory() -> PathBuf {
        let path = PathBuf::from(
            std::env::var_os("TOBKIRI_STREAM_NORMALIZER_ACTUAL_DIR")
                .expect("set TOBKIRI_STREAM_NORMALIZER_ACTUAL_DIR to a NEW absolute directory"),
        );
        assert!(path.is_absolute(), "actual directory path must be absolute");
        assert!(
            path.components()
                .all(|part| !matches!(part, Component::ParentDir)),
            "actual directory path must not contain parent traversal"
        );
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt as _;
            builder.mode(0o700);
        }
        builder
            .create(&path)
            .expect("actual directory must be new; existing evidence is never overwritten");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                fs::metadata(&path)
                    .expect("actual directory metadata")
                    .permissions()
                    .mode()
                    & 0o077,
                0,
                "actual directory must not be accessible by group or other users"
            );
        }
        path
    }

    fn write_actual(directory: &Path, path: &str, encoding: &str, bytes: &[u8]) -> PyValue {
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        let mut file = options
            .open(directory.join(path))
            .expect("create exclusive actual sidecar");
        file.write_all(bytes).expect("write exact actual bytes");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            assert_eq!(
                file.metadata()
                    .expect("actual sidecar metadata")
                    .permissions()
                    .mode()
                    & 0o077,
                0,
                "actual sidecar must not be accessible by group or other users"
            );
        }
        PyValue::object([
            ("path".into(), PyValue::String(path.into())),
            ("encoding".into(), PyValue::String(encoding.into())),
            ("bytes".into(), PyValue::Int(bytes.len().to_string())),
            ("sha256".into(), PyValue::String(sha256_hex(bytes))),
        ])
    }

    fn read_receipt_path(directory: &Path, relative: &str, limit: u64) -> Vec<u8> {
        let relative_path = Path::new(relative);
        assert!(relative_path.is_relative(), "sidecar path must be relative");
        let components = relative_path.components().collect::<Vec<_>>();
        assert!(!components.is_empty(), "sidecar path must not be empty");
        assert!(
            components
                .iter()
                .all(|component| matches!(component, Component::Normal(_))),
            "sidecar path must not traverse outside the receipt directory"
        );

        let mut path = directory.to_path_buf();
        let component_count = components.len();
        for (index, component) in components.into_iter().enumerate() {
            let Component::Normal(name) = component else {
                unreachable!("component kinds were validated above")
            };
            path.push(name);
            let metadata = fs::symlink_metadata(&path).expect("sidecar path must exist");
            assert!(
                !metadata.file_type().is_symlink(),
                "sidecar path must not contain symlinks"
            );
            if index + 1 != component_count {
                assert!(metadata.is_dir(), "sidecar parent must be a directory");
            }
        }
        read_bounded_file(&path, limit)
    }

    fn error_code_name(code: &ErrorCode) -> &'static str {
        match code {
            ErrorCode::Invalid => "invalid",
            ErrorCode::Internal => "internal",
            ErrorCode::LimitExceeded => "limit-exceeded",
            _ => "unexpected",
        }
    }

    fn many_event_payload(count: usize, terminal_at: Option<usize>) -> String {
        let mut events = String::new();
        for index in 0..count {
            if index > 0 {
                events.push(',');
            }
            let event_type = if Some(index) == terminal_at {
                "finish"
            } else if index > terminal_at.unwrap_or(usize::MAX) {
                "unknown"
            } else {
                "text_delta"
            };
            events.push_str(r#"{"type":""#);
            events.push_str(event_type);
            events.push_str(r#""}"#);
        }
        format!(r#"{{"request_id":"r","value":[{events}]}}"#)
    }

    #[test]
    fn projection_preserves_field_order_nested_data_and_terminal_sequence() {
        let payload = r#"{"request_id":" \u2003req-1\u00a0 ","provider_attempt":2.9,"value":{"events":[{"type":"text_delta","delta":{"z":1,"a":true},"sequence":99},{"type":"finish","finish_reason":"stop","extra":"discard"}]}}"#;
        let result = normalize_data("normalize", payload).unwrap();
        assert_eq!(
            result,
            r#"{"events":[{"request_id":"req-1","sequence":0,"type":"text_delta","delta":{"z":1,"a":true},"tool_intent":null,"usage":null,"finish_reason":null,"error_code":null,"provider_attempt":2},{"request_id":"req-1","sequence":1,"type":"finish","delta":null,"tool_intent":null,"usage":null,"finish_reason":"stop","error_code":null,"provider_attempt":2}]}"#
        );
    }

    #[test]
    fn runtime_identity_names_this_rust_candidate_without_a_python_claim() {
        let info = loads(&runtime_info_data()).expect("runtime info must be JSON");
        assert_eq!(
            string(field(&info, "implementation"), "implementation"),
            "rust"
        );
        assert_eq!(string(field(&info, "pack_id"), "pack_id"), super::PACK_ID);
        assert!(info.get("python").is_none());
    }

    #[test]
    fn operation_aliases_share_the_original_projection() {
        let payload = r#"{"request_id":"r","value":[{"type":"error","error_code":"failed"}]}"#;
        let expected = normalize_data("normalize", payload).unwrap();
        assert_eq!(normalize_data("validate", payload).unwrap(), expected);
        assert_eq!(
            normalize_data("rumi_ai_stream_pack.ai-stream-normalize", payload).unwrap(),
            expected
        );
    }

    #[test]
    fn terminal_failure_does_not_append_or_accept_a_later_event() {
        let payload = r#"{"request_id":"r","value":[{"type":"error"},{"type":"finish"}]}"#;
        assert_eq!(
            normalize_data("normalize", payload),
            Err(Failure::Validation)
        );
    }

    #[test]
    fn source_validation_and_input_failures_remain_distinct() {
        assert_eq!(normalize_data("unknown", "{}"), Err(Failure::Input));
        assert_eq!(normalize_data("normalize", "[]"), Err(Failure::Input));
        assert_eq!(
            normalize_data("normalize", r#"{"request_id":"r","value":[]}"#),
            Err(Failure::Validation)
        );
    }

    #[test]
    fn trimmed_empty_request_id_is_provider_validation() {
        let payload = r#"{"request_id":"\u2003\u00a0","value":[{"type":"finish"}]}"#;
        assert_eq!(
            normalize_data("normalize", payload),
            Err(Failure::Validation)
        );
    }

    #[test]
    fn output_codec_failures_follow_the_original_adapter_categories() {
        for error in [
            CodecError::Invalid,
            CodecError::ByteLimit,
            CodecError::NodeLimit,
            CodecError::IntegerLimit,
        ] {
            assert_eq!(map_output_error(error), Failure::Output);
        }
        assert_eq!(map_output_error(CodecError::NonFinite), Failure::Arithmetic);
        assert_eq!(map_output_error(CodecError::Resource), Failure::Limit);
        assert_eq!(map_input_error(CodecError::Resource), Failure::Limit);
        assert_eq!(map_input_error(CodecError::ByteLimit), Failure::Input);
    }

    #[test]
    fn guaranteed_output_limit_waits_until_stream_validation_finishes() {
        let valid_large_stream = many_event_payload(5_264, Some(5_263));
        assert_eq!(
            normalize_data("normalize", &valid_large_stream),
            Err(Failure::Output)
        );

        let invalid_after_terminal = many_event_payload(5_264, Some(5_262));
        assert_eq!(
            normalize_data("normalize", &invalid_after_terminal),
            Err(Failure::Validation)
        );
    }

    #[test]
    fn source_generated_python_oracle_receipt_matches_exact_wit_observations() {
        let Some(receipt_path) = std::env::var_os("TOBKIRI_PYTHON_COMPAT_RECEIPT") else {
            eprintln!(
                "source differential skipped; set TOBKIRI_PYTHON_COMPAT_RECEIPT to a generated receipt.json"
            );
            return;
        };
        let receipt_path = PathBuf::from(receipt_path);
        let receipt_bytes = read_bounded_file(&receipt_path, MAX_RECEIPT_BYTES);
        let receipt_text = std::str::from_utf8(&receipt_bytes).expect("receipt must be UTF-8");
        let receipt = loads(receipt_text).expect("receipt must use bounded ordered JSON");
        assert_eq!(
            string(field(&receipt, "schema"), "receipt.schema"),
            "io.tobkiri.python-provider-compat.v1"
        );
        assert_eq!(
            string(
                field(field(&receipt, "runtime"), "implementation"),
                "receipt.runtime.implementation"
            ),
            "cpython"
        );
        let runtime = field(&receipt, "runtime");
        assert_eq!(
            size(
                field(field(runtime, "python"), "major"),
                "runtime.python.major"
            ),
            3
        );
        assert_eq!(
            size(
                field(field(runtime, "python"), "minor"),
                "runtime.python.minor"
            ),
            13
        );
        assert_eq!(
            string(field(runtime, "unicode_version"), "runtime.unicode_version"),
            "15.1.0"
        );
        assert_eq!(
            size(
                field(runtime, "int_max_str_digits"),
                "runtime.int_max_str_digits"
            ),
            4300
        );

        let source_manifest = string(
            field(&receipt, "source_manifest_sha256"),
            "receipt.source_manifest_sha256",
        );
        assert_eq!(source_manifest.len(), 64);
        assert!(source_manifest.bytes().all(|byte| byte.is_ascii_hexdigit()));
        // The oracle uses json.dumps(..., sort_keys=True, separators=(',', ':'))
        // for this digest. Rebuild each source record in sorted-key order before
        // using the shared codec, which otherwise preserves insertion order.
        // Also verify every source against the checked-out original sources;
        // the receipt's hash alone only binds the manifest's claimed hashes.
        let sources = field(&receipt, "sources")
            .as_array()
            .expect("receipt.sources must be an array");
        assert!(sources.len() <= MAX_SOURCES);
        let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("Pack directory")
            .parent()
            .expect("repository directory");
        let mut source_paths = BTreeSet::new();
        let mut previous_source_path = "";
        let canonical_sources = sources
            .iter()
            .map(|source| {
                let source_path = string(field(source, "path"), "source.path");
                assert!(source_path.is_ascii(), "source paths must be ASCII");
                assert!(
                    previous_source_path < source_path,
                    "source records must be uniquely path-sorted"
                );
                previous_source_path = source_path;
                assert!(
                    source_paths.insert(source_path.to_owned()),
                    "duplicate source path: {source_path}"
                );
                let source_bytes = read_receipt_path(repository, source_path, MAX_SOURCE_BYTES);
                assert_eq!(
                    source_bytes.len() as u64,
                    size(field(source, "bytes"), "source.bytes"),
                    "source {source_path}: byte length changed"
                );
                assert_eq!(
                    sha256_hex(&source_bytes),
                    string(field(source, "sha256"), "source.sha256"),
                    "source {source_path}: hash changed"
                );
                let mut entries = source
                    .as_object()
                    .expect("receipt source record must be an object")
                    .to_vec();
                entries.sort_by(|left, right| left.0.cmp(&right.0));
                PyValue::Object(entries)
            })
            .collect::<Vec<_>>();
        let source_bytes = dumps(&PyValue::Array(canonical_sources))
            .expect("receipt source manifest must be bounded JSON");
        assert_eq!(sha256_hex(source_bytes.as_bytes()), source_manifest);
        for required_source in [
            "packs/local.ai-stream-normalizer-python/python/original_stream_normalizer.py",
            "packs/local.ai-stream-normalizer-python/python/stream_normalizer_adapter.py",
            "packs/local.ai-stream-normalizer-python/python/app.py",
            "packs/local.ai-stream-normalizer-python/python/provenance.json",
            "tobkiri_runtime/ecosystem/rumi_ai_stream_pack/runtime/normalizer.py",
            "tobkiri_runtime/core_runtime/global_contract_dispatch.py",
            "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
            "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
            "crates/tobkiri-wit/wit/core/core.wit",
            "tools/quality/python_provider_compat/generate.py",
            "tools/quality/python_provider_compat/cases.py",
        ] {
            assert!(
                source_paths.contains(required_source),
                "original source missing: {required_source}"
            );
        }

        let sidecar_root = receipt_path
            .parent()
            .expect("receipt path must have a parent directory");
        let cases = field(&receipt, "cases")
            .as_array()
            .expect("receipt.cases must be an array");
        assert!(
            cases.len() <= MAX_CASES,
            "oracle case count exceeds its bound"
        );
        let actual_directory = new_actual_directory();
        let mut checked = 0usize;
        let mut mismatches = 0usize;
        let mut successes = 0usize;
        let mut expected_errors = 0usize;
        let mut total_input_bytes = 0u64;
        let mut total_expected_output_bytes = 0u64;
        let mut seen_ids = BTreeSet::new();
        let mut operations = BTreeSet::new();
        let mut actual_records = Vec::new();
        for case in cases {
            if string(field(case, "provider"), "case.provider") != "stream-normalizer" {
                continue;
            }

            let id = string(field(case, "id"), "case.id");
            assert!(id.len() <= 128, "case ID exceeds its byte bound");
            assert!(
                !id.is_empty()
                    && id.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'
                    }),
                "case ID must be an ASCII slug"
            );
            assert!(seen_ids.insert(id.to_owned()), "duplicate case ID: {id}");
            let operation = string(field(case, "operation"), "case.operation");
            assert!(operation.len() <= 128, "operation exceeds its byte bound");
            operations.insert(operation.to_owned());
            let operation_record = field(case, "operation_utf8");
            assert_eq!(
                size(field(operation_record, "bytes"), "operation_utf8.bytes"),
                operation.len() as u64,
                "case {id}: operation byte length changed"
            );
            assert_eq!(
                string(field(operation_record, "sha256"), "operation_utf8.sha256"),
                sha256_hex(operation.as_bytes()),
                "case {id}: operation hash changed"
            );
            assert_eq!(
                string(
                    field(case, "source_manifest_sha256"),
                    "case.source_manifest_sha256"
                ),
                source_manifest,
                "case {id}: source manifest differs from receipt"
            );

            let input_record = field(case, "input");
            assert_eq!(
                string(field(input_record, "encoding"), "input.encoding"),
                "utf-8",
                "case {id}: unsupported input encoding"
            );
            let input_path = string(field(input_record, "path"), "input.path");
            let input_bytes = read_receipt_path(sidecar_root, input_path, MAX_CASE_INPUT_BYTES);
            total_input_bytes = total_input_bytes
                .checked_add(input_bytes.len() as u64)
                .expect("total input byte count overflowed");
            assert!(total_input_bytes <= 16 * 1024 * 1024);
            assert_eq!(
                input_bytes.len() as u64,
                size(field(input_record, "bytes"), "input.bytes"),
                "case {id}: input byte length changed"
            );
            assert_eq!(
                sha256_hex(&input_bytes),
                string(field(input_record, "sha256"), "input.sha256"),
                "case {id}: input hash changed"
            );
            let payload = std::str::from_utf8(&input_bytes)
                .unwrap_or_else(|_| panic!("case {id}: input is not UTF-8"));

            let expected = field(case, "expected");
            let expected_kind = string(field(expected, "kind"), "expected.kind");
            let expected_output = match expected_kind {
                "result" => {
                    successes += 1;
                    let output_record = field(expected, "output");
                    assert_eq!(
                        string(field(output_record, "encoding"), "output.encoding"),
                        "utf-8",
                        "case {id}: unsupported output encoding"
                    );
                    let output_path = string(field(output_record, "path"), "output.path");
                    let output =
                        read_receipt_path(sidecar_root, output_path, MAX_CASE_OUTPUT_BYTES);
                    assert_eq!(
                        output.len() as u64,
                        size(field(output_record, "bytes"), "output.bytes"),
                        "case {id}: output byte length changed"
                    );
                    assert_eq!(
                        sha256_hex(&output),
                        string(field(output_record, "sha256"), "output.sha256"),
                        "case {id}: output hash changed"
                    );
                    total_expected_output_bytes = total_expected_output_bytes
                        .checked_add(output.len() as u64)
                        .expect("total expected output byte count overflowed");
                    assert!(total_expected_output_bytes <= 8 * 1024 * 1024);
                    Some(output)
                }
                "error" => {
                    expected_errors += 1;
                    let code = string(field(expected, "code"), "expected.code");
                    assert!(matches!(code, "invalid" | "internal" | "limit-exceeded"));
                    let message = string(field(expected, "message"), "expected.message");
                    assert!(message.len() <= 256, "expected error message exceeds bound");
                    None
                }
                other => panic!("case {id}: unknown expected kind {other}"),
            };
            let actual = normalize_data(operation, payload).map_err(Failure::into_wit_error);

            let sequence = actual_records.len();
            let (matched, actual_record) = match actual {
                Ok(actual_output) => {
                    assert!(actual_output.len() <= MAX_BYTES);
                    let matched = expected_output
                        .as_ref()
                        .is_some_and(|expected| actual_output.as_bytes() == expected.as_slice());
                    let descriptor = write_actual(
                        &actual_directory,
                        &format!("{sequence:03}.output"),
                        "utf-8",
                        actual_output.as_bytes(),
                    );
                    (
                        matched,
                        PyValue::object([
                            ("kind".into(), PyValue::String("result".into())),
                            ("output".into(), descriptor),
                        ]),
                    )
                }
                Err(actual_error) => {
                    let actual_code = error_code_name(&actual_error.code);
                    let matched = expected_kind == "error"
                        && actual_code == string(field(expected, "code"), "expected.code")
                        && actual_error.message
                            == string(field(expected, "message"), "expected.message");
                    let error_data = PyValue::object([
                        ("code".into(), PyValue::String(actual_code.into())),
                        (
                            "message".into(),
                            PyValue::String(actual_error.message.clone()),
                        ),
                    ]);
                    let error_bytes =
                        dumps(&error_data).expect("closed WIT code/message must fit bounded JSON");
                    let descriptor = write_actual(
                        &actual_directory,
                        &format!("{sequence:03}.error.json"),
                        "utf-8",
                        error_bytes.as_bytes(),
                    );
                    (
                        matched,
                        PyValue::object([
                            ("kind".into(), PyValue::String("error".into())),
                            ("code".into(), PyValue::String(actual_code.into())),
                            ("message".into(), PyValue::String(actual_error.message)),
                            ("sidecar".into(), descriptor),
                        ]),
                    )
                }
            };
            mismatches += usize::from(!matched);
            actual_records.push(PyValue::object([
                ("id".into(), PyValue::String(id.into())),
                ("operation".into(), PyValue::String(operation.into())),
                ("input".into(), input_record.clone()),
                ("matched".into(), PyValue::Bool(matched)),
                ("expected".into(), expected.clone()),
                ("actual".into(), actual_record),
            ]));
            checked += 1;
        }
        let summary = PyValue::object([
            (
                "schema".into(),
                PyValue::String("io.tobkiri.stream-normalizer-rust-actuals.v1".into()),
            ),
            (
                "scope".into(),
                PyValue::String(
                    "native source differential only; not Wasm execution, GUI approval parity, Setup readiness, or production activation".into(),
                ),
            ),
            (
                "oracle_receipt_sha256".into(),
                PyValue::String(sha256_hex(&receipt_bytes)),
            ),
            (
                "source_manifest_sha256".into(),
                PyValue::String(source_manifest.into()),
            ),
            ("original_runtime".into(), field(&receipt, "runtime").clone()),
            (
                "case_count".into(),
                PyValue::Int(actual_records.len().to_string()),
            ),
            ("mismatch_count".into(), PyValue::Int(mismatches.to_string())),
            ("success_count".into(), PyValue::Int(successes.to_string())),
            (
                "expected_error_count".into(),
                PyValue::Int(expected_errors.to_string()),
            ),
            ("cases".into(), PyValue::Array(actual_records)),
        ]);
        let summary_bytes = dumps(&summary).expect("bounded actual summary JSON");
        write_actual(
            &actual_directory,
            "summary.json",
            "utf-8",
            summary_bytes.as_bytes(),
        );
        eprintln!(
            "native stream differential actuals retained at {}",
            actual_directory.display()
        );
        assert!(checked > 0, "receipt contains no stream-normalizer cases");
        assert!(successes > 0 && expected_errors > 0);
        for operation in [
            "normalize",
            "validate",
            "rumi_ai_stream_pack.ai-stream-normalize",
        ] {
            assert!(
                operations.contains(operation),
                "missing operation alias case: {operation}"
            );
        }
        assert_eq!(
            mismatches,
            0,
            "all stream cases compared; actuals and summary retained in {}",
            actual_directory.display()
        );
    }
}
