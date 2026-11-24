//! Replay a separate original-Python v2 receipt. The historical v1 fixture is
//! exercised by the library tests and is never reinterpreted as v2 expectations.
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{Read as _, Write as _},
    path::{Component, Path, PathBuf},
};
use tobkiri_pack_ai_tool_bridge_rust::invoke;

fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn text<'a>(value: &'a Value, field: &str) -> &'a str {
    value[field]
        .as_str()
        .unwrap_or_else(|| panic!("missing string {field}"))
}
fn read_file(path: &Path, maximum: u64) -> Vec<u8> {
    assert!(fs::symlink_metadata(path).unwrap().file_type().is_file());
    let file = File::open(path).unwrap();
    let metadata = file.metadata().unwrap();
    assert!(metadata.is_file() && metadata.len() <= maximum);
    let mut bytes = Vec::new();
    file.take(maximum + 1).read_to_end(&mut bytes).unwrap();
    assert!(bytes.len() as u64 <= maximum);
    assert_eq!(bytes.len() as u64, metadata.len());
    bytes
}
fn read_relative(root: &Path, relative: &str, maximum: u64) -> Vec<u8> {
    let path = Path::new(relative);
    assert!(path.is_relative());
    let mut checked = root.to_path_buf();
    let mut components = 0;
    for component in path.components() {
        let Component::Normal(component) = component else {
            panic!("unsafe receipt path")
        };
        checked.push(component);
        assert!(
            !fs::symlink_metadata(&checked)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        components += 1;
    }
    assert!(components != 0);
    read_file(&checked, maximum)
}
fn sidecar(root: &Path, record: &Value, maximum: u64) -> Vec<u8> {
    assert_eq!(record["encoding"], "utf-8");
    let bytes = read_relative(root, text(record, "path"), maximum);
    assert_eq!(record["bytes"].as_u64().unwrap(), bytes.len() as u64);
    assert_eq!(text(record, "sha256"), sha256(&bytes));
    bytes
}
fn actual_directory() -> PathBuf {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_TOOL_BRIDGE_ACTUAL_DIR")
            .expect("set a NEW private actual directory whenever a v2 receipt is supplied"),
    );
    assert!(path.is_absolute());
    assert!(
        !path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
    );
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt as _;
        builder.mode(0o700);
    }
    builder
        .create(&path)
        .expect("actual directory must be NEW; preserve earlier evidence");
    path
}
fn write_actual(root: &Path, name: &str, bytes: &[u8]) -> Value {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.mode(0o600);
    }
    options
        .open(root.join(name))
        .unwrap()
        .write_all(bytes)
        .unwrap();
    json!({"path":name,"encoding":"utf-8","bytes":bytes.len(),"sha256":sha256(bytes)})
}

#[test]
fn all_original_python_v2_cases_match_or_retain_exact_mismatches() {
    let Some(receipt_path) = std::env::var_os("TOBKIRI_TOOL_BRIDGE_ORACLE_RECEIPT") else {
        eprintln!(
            "v2 differential NOT EXECUTED: set TOBKIRI_TOOL_BRIDGE_ORACLE_RECEIPT and TOBKIRI_TOOL_BRIDGE_ACTUAL_DIR"
        );
        return;
    };
    let receipt_path = PathBuf::from(receipt_path);
    let raw = read_file(&receipt_path, 1024 * 1024);
    let receipt: Value = serde_json::from_slice(&raw).unwrap();
    assert_eq!(receipt["schema"], "io.tobkiri.python-provider-compat.v1");
    assert_eq!(receipt["protocol"], "ordered-finite-json-v2");
    assert_eq!(receipt["provider"], "tool-bridge");
    assert_eq!(receipt["runtime"]["implementation"], "cpython");
    assert_eq!(receipt["runtime"]["python"]["major"], 3);
    assert_eq!(receipt["runtime"]["python"]["minor"], 13);
    assert_eq!(receipt["runtime"]["unicode_version"], "15.1.0");
    assert_eq!(receipt["runtime"]["int_max_str_digits"], 4300);
    let manifest = text(&receipt, "source_manifest_sha256");
    let sources = receipt["sources"].as_array().unwrap();
    assert!(sources.len() <= 128);
    assert_eq!(sha256(&serde_json::to_vec(sources).unwrap()), manifest);
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let mut source_paths = BTreeSet::new();
    let mut previous = "";
    for source in sources {
        let path = text(source, "path");
        assert!(path.is_ascii() && previous < path);
        previous = path;
        assert!(source_paths.insert(path));
        let bytes = read_relative(repository, path, 16 * 1024 * 1024);
        assert_eq!(source["bytes"].as_u64().unwrap(), bytes.len() as u64);
        assert_eq!(text(source, "sha256"), sha256(&bytes));
    }
    for required in [
        "packs/local.ai-tool-bridge-ordered-python/python/original_bridge.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/app_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/provenance.json",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
        "crates/tobkiri-wit/wit/core/core.wit",
        "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py",
    ] {
        assert!(
            source_paths.contains(required),
            "missing original source {required}"
        );
    }
    let root = receipt_path.parent().unwrap();
    let actual_root = actual_directory();
    let cases = receipt["cases"].as_array().unwrap();
    assert_eq!(receipt["limits"]["static_case_count"], 81);
    assert_eq!(
        cases.len(),
        81,
        "the complete original v2 corpus is required"
    );
    let mut ids = BTreeSet::new();
    let mut records = Vec::new();
    let mut mismatches = 0;
    for case in cases {
        assert_eq!(text(case, "provider"), "tool-bridge");
        let id = text(case, "id");
        assert!(ids.insert(id));
        assert_eq!(text(case, "source_manifest_sha256"), manifest);
        let operation = text(case, "operation");
        assert_eq!(
            case["operation_utf8"]["bytes"].as_u64().unwrap(),
            operation.len() as u64
        );
        assert_eq!(
            text(&case["operation_utf8"], "sha256"),
            sha256(operation.as_bytes())
        );
        let input = sidecar(root, &case["input"], 4 * 1024 * 1024 + 128);
        let expected = &case["expected"];
        let expected_bytes = if text(expected, "kind") == "result" {
            Some(sidecar(root, &expected["output"], 4 * 1024 * 1024))
        } else {
            assert_eq!(text(expected, "kind"), "error");
            None
        };
        let actual = invoke(operation, std::str::from_utf8(&input).unwrap());
        let sequence = records.len();
        let (matched, observation) = match actual {
            Ok(output) => {
                let matched = expected_bytes
                    .as_ref()
                    .is_some_and(|bytes| bytes.as_slice() == output.as_bytes());
                let descriptor = write_actual(
                    &actual_root,
                    &format!("{sequence:03}.output"),
                    output.as_bytes(),
                );
                (matched, json!({"kind":"result","output":descriptor}))
            }
            Err(error) => {
                let matched = text(expected, "kind") == "error"
                    && text(expected, "code") == error.code()
                    && text(expected, "message") == error.message();
                let value = json!({"kind":"error","code":error.code(),"message":error.message()});
                let descriptor = write_actual(
                    &actual_root,
                    &format!("{sequence:03}.error.json"),
                    &serde_json::to_vec(&value).unwrap(),
                );
                (matched, json!({"error":value,"sidecar":descriptor}))
            }
        };
        mismatches += usize::from(!matched);
        records.push(json!({"id":id,"operation":operation,"input":case["input"],"expected":expected,"actual":observation,"matched":matched}));
    }
    let summary = json!({
        "schema":"io.tobkiri.tool-bridge-rust-actuals.v1",
        "scope":"native source v2 differential; separate from frozen v1, real Wasm and native Core acceptance; production hold",
        "oracle_receipt_sha256":sha256(&raw),"source_manifest_sha256":manifest,
        "original_runtime":receipt["runtime"],"case_count":records.len(),
        "mismatch_count":mismatches,"cases":records,
    });
    write_actual(
        &actual_root,
        "summary.json",
        &serde_json::to_vec_pretty(&summary).unwrap(),
    );
    assert!(
        !ids.is_empty(),
        "no tool-bridge v2 cases in the provided receipt"
    );
    assert_eq!(
        mismatches,
        0,
        "all 81 v2 cases ran; exact actuals and mismatches retained at {}",
        actual_root.display()
    );
}
