#![cfg(all(test, unix, not(target_arch = "wasm32")))]

use super::{Failure, invoke, normalize_generate_result, parse_candidate};
use ordered_python_compat::{PyValue, dumps, loads, pyrepr};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

const ORACLE_ENV: &str = "TOBKIRI_FINALIZER_NORMALIZE_ORACLE";
const ACTUAL_DIR_ENV: &str = "TOBKIRI_FINALIZER_NORMALIZE_ACTUAL_DIR";
const RECEIPT_LIMIT: usize = 1024 * 1024;
const SOURCE_FILE_LIMIT: usize = 16 * 1024 * 1024;
const SOURCE_TOTAL_LIMIT: usize = 32 * 1024 * 1024;
const INPUT_LIMIT: usize = 256 * 1024;
const INPUT_TOTAL_LIMIT: usize = 2 * 1024 * 1024;
const OUTPUT_LIMIT: usize = 512 * 1024;
const OUTPUT_TOTAL_LIMIT: usize = 4 * 1024 * 1024;

const CASE_IDS: &[&str] = &[
    "usage-missing",
    "usage-null",
    "usage-false",
    "usage-zero-int",
    "usage-zero-float",
    "usage-empty-string",
    "usage-empty-list",
    "usage-empty-object",
    "usage-string-ab",
    "usage-string-pair-row",
    "usage-nonbmp-string-pair-row",
    "usage-object-pair-row-z-a",
    "usage-object-pair-row-a-z",
    "usage-array-pairs",
    "usage-hashable-nonstring-keys",
    "usage-duplicate-pairs-order",
    "usage-object-order-z-a",
    "usage-object-order-a-z",
    "usage-pair-row-too-short",
    "usage-pair-row-too-long",
    "usage-nonstring-then-invalid-row",
    "usage-nonstring-then-noniterable-row",
    "usage-unhashable-list-key",
    "usage-unhashable-object-key",
];

#[test]
fn fresh_python_usage_oracle_checks_projection_and_native_failure_boundary() {
    let oracle_arg = env::var_os(ORACLE_ENV).map(PathBuf::from);
    let actual_arg = env::var_os(ACTUAL_DIR_ENV).map(PathBuf::from);
    if oracle_arg.is_none() && actual_arg.is_none() {
        eprintln!("skipping source oracle consumer; {ORACLE_ENV} and {ACTUAL_DIR_ENV} are unset");
        return;
    }
    let oracle_arg = oracle_arg.expect("oracle and actual output directory must be set together");
    let actual_dir = actual_arg.expect("oracle and actual output directory must be set together");
    let (receipt_path, oracle_dir) = resolve_oracle_path(&oracle_arg);
    let receipt_bytes = read_regular_file(&receipt_path, RECEIPT_LIMIT);
    let receipt: Value = serde_json::from_slice(&receipt_bytes).expect("oracle receipt JSON");

    verify_receipt(&receipt, receipt_bytes.len());
    let workspace_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("Cargo workspace root");
    let sources = verify_sources(&receipt, &workspace_root);

    create_fresh_private_dir(&actual_dir);
    write_under(&actual_dir, Path::new("receipt.json"), &receipt_bytes);
    for (relative, bytes) in &sources {
        write_under(&actual_dir, &Path::new("sources").join(relative), bytes);
    }

    let oracle_copy_root = actual_dir.join("oracle");
    create_fresh_private_dir(&oracle_copy_root);
    let mut input_total = 0usize;
    let mut output_total = 0usize;
    let mut raw_saved_total = receipt_bytes.len();
    let mut actual_cases = Vec::with_capacity(CASE_IDS.len());
    let records = receipt["cases"].as_array().expect("receipt cases");

    for (record, expected_id) in records.iter().zip(CASE_IDS) {
        let case_id = record["id"].as_str().expect("case id");
        assert_eq!(
            case_id, *expected_id,
            "receipt case order or identity changed"
        );
        assert_eq!(
            record["source_manifest_sha256"], receipt["source_manifest_sha256"],
            "case source binding differs"
        );

        let input_bytes = copy_sidecar(
            &oracle_dir,
            &oracle_copy_root,
            &record["input"],
            &format!("cases/{case_id}.input.json"),
            INPUT_LIMIT,
        );
        input_total += input_bytes.len();
        raw_saved_total += input_bytes.len();
        let python_bytes = copy_sidecar(
            &oracle_dir,
            &oracle_copy_root,
            &record["normalizer"]["output"],
            &format!("cases/{case_id}.python-value-tree.json"),
            OUTPUT_LIMIT,
        );
        output_total += python_bytes.len();
        raw_saved_total += python_bytes.len();

        let python_observation: Value =
            serde_json::from_slice(&python_bytes).expect("Python tagged observation JSON");
        let python_kind = record["normalizer"]["kind"]
            .as_str()
            .expect("normalizer kind");
        assert_eq!(python_observation["kind"], python_kind);
        let codec_kind = record["ordered_json_v2"]["kind"]
            .as_str()
            .expect("ordered-json-v2 kind");
        let codec_bytes = match codec_kind {
            "result" | "error" => {
                let suffix = if codec_kind == "result" {
                    "ordered-json-v2.json"
                } else {
                    "ordered-json-v2-error.json"
                };
                let bytes = copy_sidecar(
                    &oracle_dir,
                    &oracle_copy_root,
                    &record["ordered_json_v2"]["output"],
                    &format!("cases/{case_id}.{suffix}"),
                    OUTPUT_LIMIT,
                );
                output_total += bytes.len();
                raw_saved_total += bytes.len();
                Some(bytes)
            }
            "not-run" => {
                assert_eq!(python_kind, "exception");
                assert!(record["ordered_json_v2"].get("output").is_none());
                None
            }
            other => panic!("unknown ordered-json-v2 outcome {other}"),
        };

        let input_text = std::str::from_utf8(&input_bytes).expect("UTF-8 oracle input");
        let input = loads(input_text).expect("ordered-finite-json-v2 input");
        let value = input.get("value").expect("source value field");
        let request_id = input
            .get("request_id")
            .and_then(PyValue::as_str)
            .expect("source request id");
        let candidate = parse_candidate(input.get("candidate").expect("source Candidate").clone())
            .expect("source Candidate shape");

        let mut actual_case = json!({"id": case_id});
        match python_kind {
            "exception" => {
                assert!(matches!(
                    normalize_generate_result(value, request_id, &candidate),
                    Err(Failure::Terminal)
                ));
                assert!(codec_bytes.is_none());
                actual_case["normalizer"] = json!({"kind": "terminal"});
            }
            "result" => {
                let expected_value = &python_observation["value"];
                let has_non_string_key = tagged_usage_has_non_string_key(expected_value);
                let (actual_value, actual_non_string_key) =
                    normalize_generate_result(value, request_id, &candidate)
                        .expect("successful Python source normalization");
                assert_eq!(actual_non_string_key, has_non_string_key);
                let actual_tag = tag_pyvalue(&actual_value);
                let actual_json = dumps(&actual_value)
                    .expect("Rust string-key normalizer projection encodes")
                    .into_bytes();

                if has_non_string_key {
                    assert_eq!(codec_kind, "error");
                    let codec_error: Value = serde_json::from_slice(
                        codec_bytes.as_deref().expect("codec error sidecar"),
                    )
                    .expect("ordered-json-v2 error sidecar JSON");
                    assert_eq!(codec_error["kind"], "error");
                    assert!(codec_error["exception_type"].is_string());
                    assert_eq!(
                        actual_tag,
                        string_key_usage_projection(expected_value),
                        "string-key projection or other normalized fields differ"
                    );
                    write_under(
                        &actual_dir,
                        Path::new(&format!(
                            "actual/cases/{case_id}.string-key-projection.ordered-json-v2.json"
                        )),
                        &actual_json,
                    );
                    actual_case["normalizer"] = json!({
                        "kind": "result",
                        "usage_has_non_string_key": true,
                        "comparison": "string-key-projection-only",
                        "output_sha256": sha256_hex(&actual_json),
                    });
                } else {
                    assert_eq!(codec_kind, "result");
                    assert_eq!(
                        actual_tag, *expected_value,
                        "normalized tagged value differs"
                    );
                    let expected_bytes = codec_bytes.as_deref().expect("codec output sidecar");
                    assert_eq!(actual_json, expected_bytes, "ordered output bytes differ");
                    write_under(
                        &actual_dir,
                        Path::new(&format!(
                            "actual/cases/{case_id}.normalized.ordered-json-v2.json"
                        )),
                        &actual_json,
                    );
                    actual_case["normalizer"] = json!({
                        "kind": "result",
                        "usage_has_non_string_key": false,
                        "comparison": "full-normalized-ordered-bytes",
                        "output_sha256": sha256_hex(&actual_json),
                    });
                }
            }
            other => panic!("unknown Python normalizer outcome {other}"),
        }

        let invoke_outcome = match (python_kind, invoke("generate", input_text)) {
            ("exception", Err(Failure::Terminal)) => "terminal-before-native-dependency",
            ("result", Err(Failure::NativeUnavailable)) => {
                assert!(
                    value
                        .get("tool_intents")
                        .and_then(PyValue::as_array)
                        .is_some_and(|items| !items.is_empty())
                );
                "native-tool-caller-unavailable-after-normalization"
            }
            (kind, Err(error)) => panic!("unexpected native invoke outcome for {kind}: {error:?}"),
            (_, Ok(_)) => panic!("native invoke unexpectedly completed without imports"),
        };
        actual_case["native_invoke"] = json!(invoke_outcome);
        let actual_case_bytes = json_bytes(&actual_case);
        write_under(
            &actual_dir,
            Path::new(&format!("actual/cases/{case_id}.outcome.json")),
            &actual_case_bytes,
        );
        actual_cases.push(actual_case);
    }

    assert_eq!(input_total, receipt["limits"]["input_bytes_total"]);
    assert_eq!(output_total, receipt["limits"]["output_bytes_total"]);
    assert!(input_total <= INPUT_TOTAL_LIMIT);
    assert!(output_total <= OUTPUT_TOTAL_LIMIT);

    let summary = json!({
        "schema": "io.tobkiri.python-finalizer-normalize-rust-consumer.v1",
        "receipt_sha256": sha256_hex(&receipt_bytes),
        "source_manifest_sha256": receipt["source_manifest_sha256"],
        "runtime": receipt["runtime"],
        "case_count": actual_cases.len(),
        "raw_oracle_bytes_saved": raw_saved_total,
        "claims": {
            "normalizer_observable_projection_checked": true,
            "native_late_failure_distinction_checked": true,
            "numeric_key_full_intermediate_parity_claimed": false,
            "complete_import_order_claimed": false,
            "core_parity_claimed": false,
            "wasm_parity_claimed": false,
        },
        "cases": actual_cases,
    });
    write_under(
        &actual_dir,
        Path::new("results.json"),
        &json_bytes(&summary),
    );
}

fn verify_receipt(receipt: &Value, receipt_bytes: usize) {
    assert!(receipt_bytes <= RECEIPT_LIMIT);
    assert_eq!(
        receipt["schema"],
        "io.tobkiri.python-finalizer-normalize-compat.v1"
    );
    assert_eq!(receipt["provider"], "gateway-finalizer-normalize");
    assert_eq!(receipt["protocol"], "ordered-finite-json-v2");
    assert_eq!(
        receipt["oracle_provenance"]["kind"],
        "fresh-independent-source-observation"
    );
    assert_eq!(
        receipt["oracle_provenance"]["historical_frozen_expected_outputs_reused"],
        false
    );
    assert_eq!(
        receipt["oracle_provenance"]["historical_observer_expected_runtime_used"],
        false
    );
    assert_eq!(receipt["runtime"]["implementation"], "CPython");
    assert_eq!(receipt["runtime"]["python"], "3.13.9");
    assert_eq!(receipt["runtime"]["unicode"], "15.1.0");
    assert_eq!(receipt["runtime"]["int_max_str_digits"], 4300);
    assert_eq!(receipt["runtime"]["recursion_limit"], 1000);
    assert_eq!(receipt["runtime"]["version_info"]["major"], 3);
    assert_eq!(receipt["runtime"]["version_info"]["minor"], 13);
    assert_eq!(receipt["runtime"]["version_info"]["micro"], 9);
    assert_eq!(receipt["runtime"]["version_info"]["releaselevel"], "final");
    assert_eq!(receipt["runtime"]["version_info"]["serial"], 0);
    assert_eq!(
        receipt["fixture"]["source_path"],
        "packs/local.ai-gateway-finalizer-python/source-cases.json"
    );
    assert_eq!(receipt["fixture"]["case_id"], "generate-tools-usage");
    assert_eq!(receipt["fixture"]["operation"], "generate");
    assert_eq!(receipt["execution_scope"]["dependency_calls_made"], false);
    assert_eq!(receipt["execution_scope"]["core_parity_claimed"], false);
    assert_eq!(receipt["execution_scope"]["wasm_parity_claimed"], false);
    assert_eq!(receipt["execution_scope"]["host_behavior_claimed"], false);

    let limits = &receipt["limits"];
    assert_eq!(limits["case_count"], CASE_IDS.len());
    assert_eq!(limits["case_count_limit"], 32);
    assert_eq!(limits["input_bytes_limit_per_case"], INPUT_LIMIT);
    assert_eq!(limits["input_bytes_limit_total"], INPUT_TOTAL_LIMIT);
    assert_eq!(limits["output_bytes_limit_per_case"], OUTPUT_LIMIT);
    assert_eq!(limits["output_bytes_limit_total"], OUTPUT_TOTAL_LIMIT);
    assert_eq!(limits["source_bytes_limit_per_file"], SOURCE_FILE_LIMIT);
    assert_eq!(limits["source_bytes_limit_total"], SOURCE_TOTAL_LIMIT);
    assert_eq!(limits["receipt_bytes_limit"], RECEIPT_LIMIT);

    let cases = receipt["cases"].as_array().expect("receipt cases");
    assert_eq!(cases.len(), CASE_IDS.len());
    let prepared = &receipt["prepared_extraction"];
    assert_eq!(
        prepared["verifier"],
        "scripts/wasm/prepare_gateway_finalizer.py::verify"
    );
    assert_eq!(
        sha256_hex(canonical_json(&prepared["provenance"]).as_bytes()),
        prepared["provenance_sha256"]
            .as_str()
            .expect("provenance digest")
    );
}

fn verify_sources(receipt: &Value, workspace_root: &Path) -> Vec<(PathBuf, Vec<u8>)> {
    let sources = receipt["sources"].as_array().expect("source manifest");
    assert!(!sources.is_empty() && sources.len() <= 128);
    assert_eq!(
        sha256_hex(canonical_json(&receipt["sources"]).as_bytes()),
        receipt["source_manifest_sha256"]
            .as_str()
            .expect("source manifest digest")
    );

    let mut previous_path: Option<&str> = None;
    let mut total = 0usize;
    let mut source_files = Vec::with_capacity(sources.len());
    let mut bindings = std::collections::BTreeMap::new();
    for source in sources {
        let path = source["path"].as_str().expect("source path");
        if let Some(previous) = previous_path {
            assert!(previous < path, "source manifest is not path-sorted");
        }
        previous_path = Some(path);
        let expected_size = source["bytes"].as_u64().expect("source byte length") as usize;
        let expected_sha = source["sha256"].as_str().expect("source digest");
        assert_eq!(expected_sha.len(), 64);
        let raw = read_relative_file(workspace_root, Path::new(path), SOURCE_FILE_LIMIT);
        assert_eq!(raw.len(), expected_size, "source size changed: {path}");
        assert_eq!(
            sha256_hex(&raw),
            expected_sha,
            "source hash changed: {path}"
        );
        total += raw.len();
        bindings.insert(path.to_owned(), (expected_size, expected_sha.to_owned()));
        source_files.push((PathBuf::from(path), raw));
    }
    assert_eq!(total, receipt["limits"]["source_bytes_total"]);
    assert!(total <= SOURCE_TOTAL_LIMIT);

    let prepared = &receipt["prepared_extraction"]["provenance"];
    for field in ["sources", "transport_dependencies"] {
        for descriptor in prepared[field].as_array().expect("prepared source list") {
            let path = descriptor["path"].as_str().expect("prepared source path");
            assert_binding_matches_descriptor(&bindings, path, descriptor);
        }
    }
    assert_binding_matches_descriptor(
        &bindings,
        "packs/local.ai-gateway-finalizer-python/python/original_gateway_finalizer.py",
        &prepared["output"],
    );
    assert_binding_matches_descriptor(
        &bindings,
        "packs/local.ai-gateway-finalizer-python/python/inventory.json",
        &prepared["inventory"],
    );
    let (_, fixture_digest) = bindings
        .get("packs/local.ai-gateway-finalizer-python/source-cases.json")
        .expect("fixture source binding");
    assert_eq!(
        fixture_digest.as_str(),
        receipt["fixture"]["source_sha256"]
            .as_str()
            .expect("fixture source digest")
    );
    source_files
}

fn assert_binding_matches_descriptor(
    bindings: &std::collections::BTreeMap<String, (usize, String)>,
    source_path: &str,
    descriptor: &Value,
) {
    let (size, digest) = bindings.get(source_path).expect("required source binding");
    assert_eq!(
        *size as u64,
        descriptor["bytes"].as_u64().expect("descriptor size")
    );
    assert_eq!(
        digest.as_str(),
        descriptor["sha256"].as_str().expect("descriptor digest")
    );
}

fn resolve_oracle_path(argument: &Path) -> (PathBuf, PathBuf) {
    assert!(argument.is_absolute(), "oracle path must be absolute");
    let metadata = fs::symlink_metadata(argument).expect("oracle path exists");
    assert!(
        !metadata.file_type().is_symlink(),
        "oracle path must not be a symlink"
    );
    if metadata.is_dir() {
        (argument.join("receipt.json"), argument.to_path_buf())
    } else {
        (
            argument.to_path_buf(),
            argument
                .parent()
                .expect("receipt parent directory")
                .to_path_buf(),
        )
    }
}

fn read_regular_file(path: &Path, limit: usize) -> Vec<u8> {
    let metadata = fs::symlink_metadata(path).expect("regular file exists");
    assert!(
        metadata.file_type().is_file(),
        "expected a regular non-symlink file"
    );
    assert!(
        metadata.len() <= limit as u64,
        "file exceeds configured read bound"
    );
    let bytes = fs::read(path).expect("read regular file");
    assert_eq!(
        bytes.len() as u64,
        metadata.len(),
        "file changed while being read"
    );
    bytes
}

fn read_relative_file(root: &Path, relative: &Path, limit: usize) -> Vec<u8> {
    let components = safe_components(relative);
    let mut path = root.to_path_buf();
    for (index, component) in components.iter().enumerate() {
        path.push(component);
        let metadata = fs::symlink_metadata(&path).expect("bound path exists");
        assert!(
            !metadata.file_type().is_symlink(),
            "source path contains a symbolic link: {}",
            relative.display()
        );
        if index + 1 < components.len() {
            assert!(metadata.is_dir(), "source parent is not a directory");
        }
    }
    read_regular_file(&path, limit)
}

fn safe_components(path: &Path) -> Vec<std::ffi::OsString> {
    assert!(!path.is_absolute(), "path must be relative");
    let components = path
        .components()
        .map(|component| match component {
            Component::Normal(part) => part.to_os_string(),
            _ => panic!("path contains traversal or a non-normal component"),
        })
        .collect::<Vec<_>>();
    assert!(!components.is_empty(), "path must not be empty");
    components
}

fn copy_sidecar(
    oracle_dir: &Path,
    copy_root: &Path,
    descriptor: &Value,
    expected_path: &str,
    limit: usize,
) -> Vec<u8> {
    assert_eq!(descriptor["encoding"], "utf-8");
    let relative = descriptor["path"].as_str().expect("sidecar path");
    assert_eq!(relative, expected_path, "unexpected oracle sidecar path");
    let bytes = read_relative_file(oracle_dir, Path::new(relative), limit);
    assert_eq!(bytes.len() as u64, descriptor["bytes"].as_u64().unwrap());
    assert_eq!(
        sha256_hex(&bytes),
        descriptor["sha256"].as_str().expect("sidecar digest")
    );
    write_under(copy_root, Path::new(relative), &bytes);
    bytes
}

fn create_fresh_private_dir(path: &Path) {
    assert!(path.is_absolute(), "actual output path must be absolute");
    assert!(
        path.components()
            .all(|component| !matches!(component, Component::ParentDir)),
        "actual output path must not contain parent traversal"
    );
    let mut builder = DirBuilder::new();
    builder.mode(0o700);
    builder
        .create(path)
        .expect("actual output directory must be new");
    assert_eq!(
        fs::metadata(path).unwrap().permissions().mode() & 0o777,
        0o700
    );
}

fn write_under(root: &Path, relative: &Path, bytes: &[u8]) {
    let components = safe_components(relative);
    let mut current = root.to_path_buf();
    for component in components.iter().take(components.len() - 1) {
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(metadata) => {
                assert!(metadata.is_dir() && !metadata.file_type().is_symlink());
                fs::set_permissions(&current, fs::Permissions::from_mode(0o700))
                    .expect("restrict output directory mode");
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let mut builder = DirBuilder::new();
                builder.mode(0o700);
                builder
                    .create(&current)
                    .expect("create private output directory");
            }
            Err(error) => panic!("inspect output directory: {error}"),
        }
    }
    current.push(components.last().expect("filename"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&current)
        .expect("create new private output file");
    file.write_all(bytes).expect("write complete output file");
    file.sync_all().expect("sync output file");
    drop(file);
    assert_eq!(
        fs::metadata(&current).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(read_regular_file(&current, bytes.len()), bytes);
}

fn tagged_usage_has_non_string_key(tagged_value: &Value) -> bool {
    let usage = tagged_usage(tagged_value);
    usage
        .as_array()
        .expect("tagged usage items")
        .iter()
        .any(|pair| pair[0]["type"] != "str")
}

fn tagged_usage(tagged_value: &Value) -> &Value {
    assert_eq!(tagged_value["type"], "dict");
    let items = tagged_value["items"]
        .as_array()
        .expect("tagged output items");
    let usage_pair = items
        .iter()
        .find(|pair| pair[0]["type"] == "str" && pair[0]["value"] == "usage")
        .expect("normalized usage field");
    assert_eq!(usage_pair[1]["type"], "dict");
    &usage_pair[1]["items"]
}

fn string_key_usage_projection(tagged_value: &Value) -> Value {
    let mut projected = tagged_value.clone();
    let items = projected["items"]
        .as_array_mut()
        .expect("tagged output items");
    let usage_pair = items
        .iter_mut()
        .find(|pair| pair[0]["type"] == "str" && pair[0]["value"] == "usage")
        .expect("normalized usage field");
    assert_eq!(usage_pair[1]["type"], "dict");
    usage_pair[1]["items"]
        .as_array_mut()
        .expect("tagged usage items")
        .retain(|pair| pair[0]["type"] == "str");
    projected
}

fn tag_pyvalue(value: &PyValue) -> Value {
    match value {
        PyValue::Null => json!({"type": "none"}),
        PyValue::Bool(value) => json!({"type": "bool", "value": value}),
        PyValue::Int(value) => json!({"type": "int", "decimal": value}),
        PyValue::Float(value) => json!({
            "type": "float",
            "repr": pyrepr(&PyValue::Float(*value)),
            "hex": python_float_hex(*value),
        }),
        PyValue::String(value) => json!({"type": "str", "value": value}),
        PyValue::Array(values) => json!({
            "type": "list",
            "items": values.iter().map(tag_pyvalue).collect::<Vec<_>>(),
        }),
        PyValue::Object(fields) => json!({
            "type": "dict",
            "items": fields.iter().map(|(key, value)| json!([
                {"type": "str", "value": key},
                tag_pyvalue(value),
            ])).collect::<Vec<_>>(),
        }),
    }
}

fn python_float_hex(value: f64) -> String {
    assert!(value.is_finite(), "source JSON contains finite floats only");
    let bits = value.to_bits();
    let sign = if bits >> 63 == 0 { "" } else { "-" };
    let exponent_bits = ((bits >> 52) & 0x7ff) as i32;
    let fraction = bits & ((1u64 << 52) - 1);
    if exponent_bits == 0 && fraction == 0 {
        return format!("{sign}0x0.0p+0");
    }
    if exponent_bits == 0 {
        return format!("{sign}0x0.{fraction:013x}p-1022");
    }
    format!("{sign}0x1.{fraction:013x}p{:+}", exponent_bits - 1023)
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn canonical_json(value: &Value) -> String {
    match value {
        Value::Null => "null".into(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => canonical_string(value),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(canonical_json)
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Object(values) => {
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|key| format!(
                        "{}:{}",
                        canonical_string(key),
                        canonical_json(&values[key])
                    ))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
    }
}

fn canonical_string(value: &str) -> String {
    let mut output = String::from("\"");
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{0008}' => output.push_str("\\b"),
            '\t' => output.push_str("\\t"),
            '\n' => output.push_str("\\n"),
            '\u{000c}' => output.push_str("\\f"),
            '\r' => output.push_str("\\r"),
            character if (character as u32) < 0x20 => {
                push_unicode_escape(&mut output, character as u16);
            }
            character if (character as u32) <= 0x7f => output.push(character),
            character if (character as u32) <= 0xffff => {
                push_unicode_escape(&mut output, character as u16);
            }
            character => {
                let scalar = character as u32 - 0x1_0000;
                push_unicode_escape(&mut output, (0xd800 + (scalar >> 10)) as u16);
                push_unicode_escape(&mut output, (0xdc00 + (scalar & 0x3ff)) as u16);
            }
        }
    }
    output.push('"');
    output
}

fn push_unicode_escape(output: &mut String, value: u16) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    output.push_str("\\u");
    for shift in [12, 8, 4, 0] {
        output.push(HEX[((value >> shift) & 0xf) as usize] as char);
    }
}

fn json_bytes(value: &Value) -> Vec<u8> {
    let mut bytes = serde_json::to_vec_pretty(value).expect("serialize observation receipt");
    bytes.push(b'\n');
    bytes
}
