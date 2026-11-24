//! Independent finite App canonical-adapter comparison, not recovery adoption.
use super::*;
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::path::PathBuf;

fn points(value: &Value) -> Result<Vec<u32>> {
    let rows = value.as_array().context("original codepoints required")?;
    ensure!(rows.len() <= 4096);
    rows.iter()
        .map(|v| {
            let cp = v.as_u64().context("unsigned codepoint required")?;
            ensure!(cp <= 0x10ffff);
            Ok(cp as u32)
        })
        .collect()
}

#[test]
#[ignore = "requires fresh independent pinned original identity observations"]
fn original_reference_driver() -> Result<()> {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_BOOTSTRAP_IDENTITY_REFERENCE")
            .context("fresh original identity reference required")?,
    );
    let bytes = std::fs::read(&path)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let reference: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        reference["format"] == "io.tobkiri.original-bootstrap-identity-reference.v1"
            && reference["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && reference["python"] == "3.13.16"
            && reference["reference_checks_passed"] == true
    );
    ensure!(
        reference["protocol_error"]["type"] == BootstrapProtocolError::CLASS
            && reference["protocol_error"]["code"] == BootstrapProtocolError::CODE
    );
    let cases = reference["cases"]
        .as_array()
        .context("original input inventory required")?;
    ensure!(cases.len() == 69);
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let mut meter = Meter::default();
    let mut observed = Vec::new();
    let mut raw_raised = 0;
    for row in cases {
        if row["raw"]["outcome"] == "getter-raised" {
            raw_raised += 1;
            continue;
        }
        ensure!(row["raw"]["outcome"] == "getter-returned");
        let name = row["name"].as_str().context("case name required")?;
        let raw = &row["raw"]["value"];
        let is_string = raw["kind"] == "str";
        let input_points = if is_string {
            points(&raw["codepoints"])?
        } else {
            Vec::new()
        };
        let input = if is_string {
            BootstrapIdentityInput::String {
                length: usize::try_from(
                    raw["length"].as_u64().context("original length required")?,
                )?
                .min(129),
                codepoints: &input_points,
            }
        } else {
            ensure!(matches!(
                raw["kind"].as_str(),
                Some(
                    "none"
                        | "bool"
                        | "int"
                        | "float"
                        | "list"
                        | "tuple"
                        | "dict"
                        | "set"
                        | "frozenset"
                )
            ));
            BootstrapIdentityInput::NonString
        };
        // CI-owned test scratch is distinct from real invocation capacity.
        // The adapter accepts only caller-provided storage, never a hidden heap.
        let mut scratch = [0; 512];
        let actual = validate_bootstrap_identity(input, &mut scratch, &erased, &mut meter)
            .map_err(|e| anyhow::anyhow!("native processing refusal in {name}: {e:?}"))?;
        let observation = match actual {
            Ok(value) => {
                ensure!(
                    row["host"]["outcome"] == "returned" && is_string,
                    "native success differs from original in {name}"
                );
                ensure!(
                    value == points(&row["host"]["value"]["codepoints"])?
                        && row["host"]["same_value"] == true
                        && std::ptr::eq::<[u32]>(value, input_points.as_slice())
                );
                json!({"name":name,"outcome":"canonical","same_input":true,"codepoints":value})
            }
            Err(error) => {
                ensure!(
                    row["host"]["outcome"] == "error",
                    "native ProtocolError differs from original success in {name}"
                );
                let host = &row["host"]["error"];
                let original = &host["cause"];
                ensure!(
                    host["kind"] == "profile-resolution-denied"
                        && host["suppress_context"] == true
                        && original["kind"] == "protocol-error"
                        && host["context"]["ref"] == original["id"]
                        && original["args"]
                            .as_array()
                            .context("original ProtocolError args required")?
                            .len()
                            == 1
                        && original["args"][0]["kind"] == "str"
                );
                let expected = points(&original["args"][0]["codepoints"])?;
                let actual: Vec<u32> = error.message.as_str()?.chars().map(u32::from).collect();
                ensure!(
                    actual == expected,
                    "native ProtocolError message differs in {name}"
                );
                drop(error);
                json!({"name":name,"outcome":"protocol-error","message_codepoints":actual})
            }
        };
        ensure!(
            budget.bytes.load(Ordering::SeqCst) == 0,
            "adapter message funding leaked in {name}"
        );
        observed.push(observation);
    }
    ensure!(observed.len() == 58 && raw_raised == 11);
    let output = json!({"format":"io.tobkiri.app-bootstrap-identity-native-comparison.v1",
        "original_head":reference["original_head"],"canonical_adapter_cases":observed.len(),
        "raw_getter_error_cases_outside_this_adapter":raw_raised,"cases":observed,
        "matched":true,"charged_message_bytes_after_drop":0,"qualification":false,
        "production_enabled":false,"scope":"finite App canonical adapter only; CI scratch/test meter; no actual invocation resource, journal, factory/cause or GUI qualification"});
    let bytes = serde_json::to_vec_pretty(&output)?;
    ensure!(bytes.len() <= 256 * 1024);
    std::fs::write(
        path.with_file_name("canonical-adapter-comparison.json"),
        bytes,
    )?;
    eprintln!(
        "APP_BOOTSTRAP_IDENTITY_COMPARISON returned_cases=58 raw_errors_outside_adapter=11 matched=true"
    );
    Ok(())
}
