//! Opt-in native comparison with a separately produced, explicitly pinned
//! original-source receipt. Every comparable row is retained before failure.
use ordered_python_compat::{PyValue, dumps, loads, pystr};
use serde::Deserialize;
use serde_json::{Value, json, value::RawValue};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, OpenOptions},
    io::{Read as _, Write as _},
    path::{Component, Path, PathBuf},
};
use tobkiri_pack_provider_model_access_rust::{OPERATIONS, invoke};

const PREFIX: &str = "TOBKIRI_PROVIDER_MODEL_ACCESS";
const CASE_SHA: &str = "sha256:ddbc327e766c8255602b3568a7886ac7dc04a2c0ccbc813896737129d4e17db9";

#[derive(Deserialize)]
struct RawReceipt {
    observations: Vec<Box<RawValue>>,
}
#[derive(Deserialize)]
struct Row {
    sequence: usize,
    id: String,
    operation: String,
    comparison: String,
    arguments_before: Box<RawValue>,
    outcome_json: String,
    outcome_python_type_tree: Value,
    outcome_mapping_key_order: Value,
    inputs_unchanged: bool,
}

fn hash(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key]
        .as_str()
        .unwrap_or_else(|| panic!("missing {key}"))
}
fn no_links(path: &Path) {
    for ancestor in path.ancestors() {
        assert!(!ancestor.is_symlink(), "symlink fixture path");
    }
}
fn read(path: &Path, maximum: u64) -> Vec<u8> {
    assert!(path.is_absolute());
    no_links(path);
    let file = fs::File::open(path).unwrap();
    let length = file.metadata().unwrap().len();
    assert!(file.metadata().unwrap().is_file() && length <= maximum);
    let mut bytes = Vec::new();
    file.take(maximum + 1).read_to_end(&mut bytes).unwrap();
    assert_eq!(bytes.len() as u64, length);
    bytes
}
fn relative(root: &Path, name: &str, maximum: u64) -> Vec<u8> {
    let path = Path::new(name);
    assert!(
        path.components().next().is_some()
            && path
                .components()
                .all(|part| matches!(part, Component::Normal(_)))
    );
    read(&root.join(path), maximum)
}
fn bound_source(root: &Path, record: &Value) {
    let bytes = relative(root, text(record, "path"), 2 * 1024 * 1024);
    assert_eq!(record["bytes"].as_u64(), Some(bytes.len() as u64));
    assert_eq!(text(record, "sha256"), hash(&bytes));
}
fn actual_directory() -> PathBuf {
    let path = PathBuf::from(
        std::env::var_os(format!("{PREFIX}_ACTUAL_DIR")).expect("NEW actual directory required"),
    );
    assert!(
        path.is_absolute()
            && path
                .components()
                .all(|part| !matches!(part, Component::ParentDir))
    );
    no_links(&path);
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(&path).expect("actual directory must be NEW");
    path
}
fn save(root: &Path, name: &str, bytes: &[u8]) -> Value {
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(root.join(name)).unwrap();
    file.write_all(bytes).unwrap();
    file.sync_all().unwrap();
    json!({"path":name,"bytes":bytes.len(),"sha256":hash(bytes)})
}

fn python_hex(value: f64) -> String {
    let bits = value.to_bits();
    let sign = if bits >> 63 == 0 { "" } else { "-" };
    let exponent = (bits >> 52) & 2047;
    let fraction = bits & ((1u64 << 52) - 1);
    if exponent == 0 && fraction == 0 {
        format!("{sign}0x0.0p+0")
    } else {
        format!(
            "{sign}0x{}.{fraction:013x}p{:+}",
            if exponent == 0 { 0 } else { 1 },
            if exponent == 0 {
                -1022
            } else {
                exponent as i32 - 1023
            }
        )
    }
}
fn type_tree(value: &PyValue) -> Value {
    match value {
        PyValue::Null => json!({"type":"NoneType"}),
        PyValue::Bool(_) => json!({"type":"bool"}),
        PyValue::Int(_) => json!({"type":"int"}),
        PyValue::Float(number) => {
            json!({"type":"float","repr":pystr(value),"hex":python_hex(*number),"finite":number.is_finite()})
        }
        PyValue::String(_) => json!({"type":"str"}),
        PyValue::Array(values) => {
            json!({"type":"list","items":values.iter().map(type_tree).collect::<Vec<_>>()})
        }
        PyValue::Object(entries) => {
            json!({"type":"dict","entries":entries.iter().map(|(key,value)|json!({"key":key,"value":type_tree(value)})).collect::<Vec<_>>()})
        }
    }
}
fn order_tree(value: &PyValue) -> Value {
    match value {
        PyValue::Object(entries) => {
            json!({"keys":entries.iter().map(|(key,_)|key).collect::<Vec<_>>(),
            "values":entries.iter().map(|(key,value)|(key.clone(),order_tree(value))).collect::<serde_json::Map<_,_>>()})
        }
        PyValue::Array(values) => json!(values.iter().map(order_tree).collect::<Vec<_>>()),
        _ => Value::Null,
    }
}

#[test]
#[ignore = "explicit original-source receipt/hash and NEW private actual directory required; native only"]
fn all_finite_original_observations_match_or_retain_exact_mismatches() {
    let receipt_path = PathBuf::from(
        std::env::var_os(format!("{PREFIX}_ORACLE_RECEIPT")).expect("original receipt required"),
    );
    let bytes = read(&receipt_path, 8 * 1024 * 1024);
    let pin = std::env::var(format!("{PREFIX}_ORACLE_SHA256"))
        .expect("explicit receipt SHA-256 required");
    assert!(
        pin.len() == 64
            && pin
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    );
    assert_eq!(hash(&bytes), format!("sha256:{pin}"));
    let receipt: Value = serde_json::from_slice(&bytes).unwrap();
    let raw: RawReceipt = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        receipt["schema"],
        "tobkiri.issue1520.provider-model-access-observations.v1"
    );
    assert_eq!(receipt["producer"], "direct-canonical-python-source-call");
    assert_eq!(receipt["runtime"]["implementation"], "cpython");
    assert_eq!(receipt["runtime"]["version_info"]["major"], 3);
    assert_eq!(receipt["runtime"]["version_info"]["minor"], 13);
    assert_eq!(receipt["runtime"]["version_info"]["micro"], 9);
    assert_eq!(receipt["runtime"]["unicode_version"], "15.1.0");
    assert_eq!(receipt["runtime"]["int_max_str_digits"], 4300);
    assert_eq!(receipt["runtime"]["recursion_limit"], 1000);
    assert_eq!(receipt["cases"]["sha256"], CASE_SHA);
    bound_source(receipt_path.parent().unwrap(), &receipt["cases"]);
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let closure = receipt["source_closure"].as_array().unwrap();
    assert_eq!(closure.len(), 7);
    let mut source_ids = BTreeSet::new();
    for source in closure {
        assert!(source_ids.insert(text(source, "path")));
        bound_source(&repository.join("tobkiri_runtime"), source);
    }
    for required in [
        "tobkiri_protocol/provider_compiler/model_access.py",
        "tobkiri_protocol/provider_compiler/native_filters.py",
    ] {
        assert!(source_ids.contains(required));
    }
    assert_eq!(raw.observations.len(), 80);
    assert_eq!(receipt["observation_count"], 80);
    let actual_dir = actual_directory();
    let receipt_identity = save(&actual_dir, "original-observations.json", &bytes);
    let mut ids = BTreeSet::new();
    let mut operations = BTreeSet::new();
    let mut records = Vec::new();
    let mut supplementary = Vec::new();
    for (index, original) in raw.observations.iter().enumerate() {
        let row: Row = serde_json::from_str(original.get()).unwrap();
        assert_eq!(row.sequence, index);
        assert!(ids.insert(row.id.clone()));
        assert!(OPERATIONS.contains(&row.operation.as_str()));
        operations.insert(row.operation.clone());
        assert!(row.inputs_unchanged);
        if row.comparison == "source-only-nonfinite" {
            supplementary.push(json!({"id":row.id,"operation":row.operation,"comparison":row.comparison,
                "native_attempted":false,"wasm_attempted":false,"original_source_observation":serde_json::from_str::<Value>(original.get()).unwrap()}));
            continue;
        }
        assert_eq!(row.comparison, "native-wasm-finite");
        // RawValue preserves Python dict order and 1.0 versus 1; serde Value's
        // default sorted map must never construct the invocation payload.
        let arguments = loads(row.arguments_before.get()).unwrap();
        let payload = dumps(&arguments).unwrap();
        let input = save(
            &actual_dir,
            &format!("case-{index:03}.input.json"),
            payload.as_bytes(),
        );
        let observed = std::panic::catch_unwind(|| invoke(&row.operation, &payload));
        let mut record = json!({"id":row.id,"operation":row.operation,"comparison":row.comparison,"native_attempted":true,
            "wasm_attempted":false,"input":input,"expected_outcome_json":row.outcome_json,"matched":false});
        match observed {
            Ok(Ok(output)) => {
                record["output"] = save(
                    &actual_dir,
                    &format!("case-{index:03}.output.json"),
                    output.as_bytes(),
                );
                let decoded = loads(&output).unwrap();
                let value = decoded.get("value");
                let types = value.map(type_tree).unwrap_or(Value::Null);
                let order = value.map(order_tree).unwrap_or(Value::Null);
                let exact = output == row.outcome_json;
                record["exact_abi_bytes"] = json!(exact);
                record["outcome_python_type_tree"] = types.clone();
                record["outcome_mapping_key_order"] = order.clone();
                record["exact_type_tree"] = json!(types == row.outcome_python_type_tree);
                record["exact_order_tree"] = json!(order == row.outcome_mapping_key_order);
                record["matched"] = json!(
                    exact
                        && types == row.outcome_python_type_tree
                        && order == row.outcome_mapping_key_order
                );
            }
            Ok(Err(error)) => {
                record["boundary_error"] = json!({"code":error.code(),"message":error.message()});
            }
            Err(_) => {
                record["unexpected_native_panic"] = json!(true);
            }
        }
        save(
            &actual_dir,
            &format!("case-{index:03}.actual.json"),
            &serde_json::to_vec_pretty(&record).unwrap(),
        );
        records.push(record);
    }
    let mismatches = records
        .iter()
        .filter(|record| record["matched"] != true)
        .count();
    let complete = records.len() == 79 && supplementary.len() == 1 && operations.len() == 8;
    let summary = json!({"schema":"tobkiri.provider-model-access-rust.native-comparison.v1","source_receipt":receipt_identity,
        "source_case_sha256":CASE_SHA,"source_observation_count":80,"finite_comparable_count":records.len(),
        "source_only_nonfinite":supplementary,"all_finite_attempted":complete,"mismatch_count":mismatches,
        "native_comparison_passed":complete&&mismatches==0,"wasm_execution_performed":false,"core_runtime_acceptance":false,
        "production_enabled":false,"full_api_settings_migration_complete":false,"cases":records});
    save(
        &actual_dir,
        "summary.json",
        &serde_json::to_vec_pretty(&summary).unwrap(),
    );
    assert!(
        complete && mismatches == 0,
        "provider compiler comparison incomplete or mismatched; all actuals retained"
    );
}
