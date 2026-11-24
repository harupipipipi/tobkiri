//! Native source differential only. Expected bytes come from the separately
//! generated original-Python receipt, never from this Rust implementation.
//! Actuals are written only into a caller-selected new directory. Existing
//! source evidence, receipts, fixtures and expected outputs are never changed.

use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    io::Write as _,
    path::{Component, Path, PathBuf},
};
use tobkiri_pack_ai_usage_cost_rust::invoke;

fn text<'a>(record: &'a Value, key: &str) -> &'a str {
    record[key]
        .as_str()
        .unwrap_or_else(|| panic!("missing receipt string: {key}"))
}

fn sha256(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn bounded_read(path: &Path, maximum: u64) -> Vec<u8> {
    assert!(fs::metadata(path).expect("evidence metadata").len() <= maximum);
    let bytes = fs::read(path).expect("read immutable evidence");
    assert!(u64::try_from(bytes.len()).unwrap() <= maximum);
    bytes
}

fn checked_file(base: &Path, record: &Value) -> Vec<u8> {
    let relative = Path::new(text(record, "path"));
    assert!(!relative.as_os_str().is_empty());
    assert!(
        relative
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
    );
    // Invalid-input vectors may deliberately exceed the product's 4 MiB cap.
    let bytes = bounded_read(&base.join(relative), 8 * 1024 * 1024);
    assert_eq!(
        u64::try_from(bytes.len()).unwrap(),
        record["bytes"].as_u64().unwrap()
    );
    assert_eq!(sha256(&bytes), text(record, "sha256"));
    bytes
}

fn new_actual_directory() -> PathBuf {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_USAGE_COST_ACTUAL_DIR")
            .expect("set TOBKIRI_USAGE_COST_ACTUAL_DIR to a NEW absolute directory"),
    );
    assert!(path.is_absolute());
    assert!(
        path.components()
            .all(|part| !matches!(part, Component::ParentDir))
    );
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt as _;
        builder.mode(0o700);
    }
    builder
        .create(&path)
        .expect("actual directory must be NEW with an existing parent; never overwrite evidence");
    path
}

fn write_actual(directory: &Path, path: &str, encoding: &str, bytes: &[u8]) -> Value {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.mode(0o600);
    }
    options
        .open(directory.join(path))
        .expect("create exclusive actual sidecar")
        .write_all(bytes)
        .expect("write exact actual bytes");
    json!({"path":path,"encoding":encoding,"bytes":bytes.len(),"sha256":sha256(bytes)})
}

#[test]
fn original_python_receipt_matches_every_usage_cost_case_byte_for_byte() {
    let receipt_path = PathBuf::from(
        std::env::var_os("TOBKIRI_USAGE_COST_ORACLE_RECEIPT")
            .expect("set TOBKIRI_USAGE_COST_ORACLE_RECEIPT to an original-Python receipt.json"),
    );
    let receipt_dir = receipt_path.parent().expect("receipt directory");
    let receipt_bytes = bounded_read(&receipt_path, 1024 * 1024);
    let receipt: Value = serde_json::from_slice(&receipt_bytes).unwrap();
    assert_eq!(
        text(&receipt, "schema"),
        "io.tobkiri.python-provider-compat.v1"
    );
    let source_manifest = text(&receipt, "source_manifest_sha256");
    assert!(
        source_manifest.len() == 64 && source_manifest.bytes().all(|byte| byte.is_ascii_hexdigit())
    );
    assert_eq!(text(&receipt["runtime"], "implementation"), "cpython");
    assert_eq!(receipt["runtime"]["python"]["major"], 3);
    assert_eq!(receipt["runtime"]["python"]["minor"], 13);
    assert_eq!(text(&receipt["runtime"], "unicode_version"), "15.1.0");
    assert_eq!(receipt["runtime"]["int_max_str_digits"], 4300);
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let mut sources = BTreeSet::new();
    let mut source_rows = Vec::new();
    for source in receipt["sources"]
        .as_array()
        .expect("bound original sources")
    {
        checked_file(repository, source);
        let path = text(source, "path");
        assert!(sources.insert(path), "duplicate bound source: {path}");
        // The source manifest uses Python's compact, sorted-key ASCII JSON.
        // Canonical source paths and hashes are ASCII; object-key order here
        // is explicit and does not depend on serde_json map feature flags.
        assert!(path.is_ascii());
        source_rows.push((
            path,
            format!(
                "{{\"bytes\":{},\"path\":{},\"sha256\":{}}}",
                source["bytes"].as_u64().unwrap(),
                serde_json::to_string(path).unwrap(),
                serde_json::to_string(text(source, "sha256")).unwrap(),
            ),
        ));
    }
    source_rows.sort_unstable_by_key(|(path, _)| *path);
    let source_json = format!(
        "[{}]",
        source_rows
            .iter()
            .map(|(_, row)| row.as_str())
            .collect::<Vec<_>>()
            .join(",")
    );
    assert_eq!(sha256(source_json.as_bytes()), source_manifest);
    for required in [
        "packs/local.ai-usage-cost-python/python/original_usage_cost.py",
        "packs/local.ai-usage-cost-python/python/usage_cost_adapter.py",
        "packs/local.ai-usage-cost-python/python/app.py",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
        "packs/local.ai-usage-cost-python/python/provenance.json",
        "tobkiri_runtime/ecosystem/rumi_ai_usage_pack/runtime/usage.py",
        "crates/tobkiri-wit/wit/core/core.wit",
        "tools/quality/python_provider_compat/generate.py",
        "tools/quality/python_provider_compat/cases.py",
    ] {
        assert!(
            sources.contains(required),
            "original source missing: {required}"
        );
    }
    let mut ids = BTreeSet::new();
    let mut operations = BTreeSet::new();
    let mut successes = 0;
    let mut arithmetic_errors = 0;
    let cases = receipt["cases"].as_array().expect("source cases");
    assert!(
        cases.len() <= 128,
        "original oracle bounds at most 128 cases"
    );
    let actual_directory = new_actual_directory();
    let mut records = Vec::new();
    let mut mismatches = 0;
    for case in cases {
        if text(case, "provider") != "usage-cost" {
            continue;
        }
        let id = text(case, "id");
        assert!(id.len() <= 128);
        assert!(
            id.bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        );
        assert!(ids.insert(id), "duplicate usage case identity: {id}");
        assert_eq!(text(case, "source_manifest_sha256"), source_manifest);
        let operation = text(case, "operation");
        operations.insert(operation);
        assert_eq!(text(&case["input"], "encoding"), "utf-8");
        let input = checked_file(receipt_dir, &case["input"]);
        let actual = invoke(operation, std::str::from_utf8(&input).unwrap());
        let expected = &case["expected"];
        let expected_output = match text(expected, "kind") {
            "result" => {
                successes += 1;
                assert_eq!(text(&expected["output"], "encoding"), "utf-8");
                Some(checked_file(receipt_dir, &expected["output"]))
            }
            "error" => {
                arithmetic_errors += usize::from(
                    text(expected, "message") == "usage cost provider arithmetic failed",
                );
                None
            }
            _ => panic!("case {id}: unsupported expectation kind"),
        };
        let sequence = records.len();
        let (matched, actual_record) = match actual {
            Ok(output) => {
                assert!(output.len() <= ordered_python_compat::MAX_BYTES);
                let matched = expected_output
                    .as_ref()
                    .is_some_and(|expected| output.as_bytes() == expected.as_slice());
                let descriptor = write_actual(
                    &actual_directory,
                    &format!("{sequence:03}.output"),
                    "utf-8",
                    output.as_bytes(),
                );
                if !matched {
                    eprintln!("{id}: actual sha256 {}", text(&descriptor, "sha256"));
                }
                (matched, json!({"kind":"result","output":descriptor}))
            }
            Err(error) => {
                let matched = expected_output.is_none()
                    && error.code() == text(expected, "code")
                    && error.message() == text(expected, "message");
                let actual = json!({"kind":"error","code":error.code(),"message":error.message()});
                let descriptor = write_actual(
                    &actual_directory,
                    &format!("{sequence:03}.error.json"),
                    "ascii-json",
                    &serde_json::to_vec(&actual).unwrap(),
                );
                if !matched {
                    eprintln!("{id}: actual {} / {}", error.code(), error.message());
                }
                (
                    matched,
                    json!({"kind":"error","code":error.code(),"message":error.message(),"sidecar":descriptor}),
                )
            }
        };
        mismatches += usize::from(!matched);
        records.push(json!({
            "id":id,"operation":operation,"matched":matched,
            "expected":expected,"actual":actual_record,
        }));
    }
    let summary = json!({
        "schema":"io.tobkiri.usage-cost-rust-actuals.v1",
        "scope":"native source differential only; no Wasm or production activation claim",
        "oracle_receipt_sha256":sha256(&receipt_bytes),
        "source_manifest_sha256":source_manifest,
        "original_runtime":receipt["runtime"],
        "case_count":records.len(),"mismatch_count":mismatches,"cases":records,
    });
    write_actual(
        &actual_directory,
        "summary.json",
        "ascii-json",
        &serde_json::to_vec(&summary).unwrap(),
    );
    assert!(successes > 0 && arithmetic_errors > 0);
    for operation in [
        "calculate",
        "normalize",
        "rumi_ai_usage_pack.ai-usage-cost",
        "rumi_ai_usage_pack.ai-usage-cost.generate",
        "rumi_ai_usage_pack.ai-usage-cost.stream",
    ] {
        assert!(
            operations.contains(operation),
            "missing alias vector: {operation}"
        );
    }
    assert_eq!(
        mismatches, 0,
        "all usage cases ran; actuals and mismatches retained"
    );
}
