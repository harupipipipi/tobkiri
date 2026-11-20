//! Replay the original Workflow messages guest through the staged Wasm archive.

use anyhow::{Context, Result, bail, ensure};
use serde_json::Value;
use std::{collections::BTreeMap, fs, sync::Arc};
use tobkiri_canon::{
    Digest,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_core::pack::verify_tpk;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};
use tobkiri_wasm::WasmEngine;
use wasmtime::component::{BudgetedString, Val};

struct WorkflowMessagesOwner;

impl NativeSetupLease for WorkflowMessagesOwner {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"Workflow messages comparison")
    }
}

#[tokio::test]
#[ignore = "requires the staged Workflow messages archive and original CPython reference"]
async fn workflow_messages_replays_original_guest() -> Result<()> {
    let archive = std::env::var_os("TOBKIRI_WORKFLOW_MESSAGES_TPK")
        .context("set TOBKIRI_WORKFLOW_MESSAGES_TPK")?;
    let reference = std::env::var_os("TOBKIRI_WORKFLOW_MESSAGES_REFERENCE")
        .context("set TOBKIRI_WORKFLOW_MESSAGES_REFERENCE")?;
    let cases: Vec<Value> = serde_json::from_slice(&fs::read(reference)?)?;
    for (required, accepted) in [
        ("input_exact_limit", true),
        ("input_over_limit", false),
        ("output_exact_limit", true),
        ("output_over_limit", false),
        ("depth_exact_limit", true),
        ("depth_over_limit", false),
        ("forged_workspace_resolution", false),
        ("rich_selected_history", true),
        ("owner_timing_microseconds", true),
    ] {
        let mut matching = cases.iter().filter(|case| case["name"] == required);
        let case = matching
            .next()
            .with_context(|| format!("reference must cover {required}"))?;
        ensure!(matching.next().is_none(), "reference repeats {required}");
        ensure!(
            case.get("output")
                .is_some_and(|output| output.is_object() == accepted),
            "reference has the wrong expected outcome for {required}"
        );
    }
    let pack = verify_tpk(&fs::read(archive)?, &BTreeMap::new())?;
    ensure!(pack.manifest.id.as_str() == "local.workflow-messages");
    ensure!(pack.manifest.version == "0.1.0");

    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(WorkflowMessagesOwner)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let profile = ProfileId::parse("defaults")?;
    let shape = ShapeRef::parse("local.workflow-messages.data@1.0")?;
    let operation = OpName::parse("build")?;

    for case in &cases {
        let name = case["name"].as_str().context("reference case name")?;
        let expected = case.get("output").context("reference output")?;
        let input = match case.get("input").context("reference input")? {
            Value::String(raw) => raw.clone(),
            document => document.to_string(),
        };
        match name {
            "input_exact_limit" => ensure!(input.len() == 512 * 1024),
            "input_over_limit" => ensure!(input.len() == 512 * 1024 + 1),
            _ => {}
        }
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let port = wasm
            .prepare_declared_read_only_port(&pack, &shape, &operation, &profile, deadline, 0)
            .await
            .with_context(|| format!("prepare Workflow messages for {name}"))?;
        let input = BudgetedString::copy_from(&input, port.allocation_owners().component(), 1)?;
        let result = port
            .invoke_with_document_limit(&[Val::String(input)])
            .await
            .with_context(|| format!("execute Workflow messages for {name}"))?;
        match result.as_slice() {
            [Val::Result(Ok(Some(value)))] => {
                ensure!(expected.is_object(), "{name} expected a rejection");
                let Val::String(text) = &**value else {
                    bail!("{name} returned the wrong document type");
                };
                if name == "output_exact_limit" {
                    ensure!(text.as_str().len() == 512 * 1024);
                }
                let actual: Value = serde_json::from_str(text.as_str())?;
                ensure!(&actual == expected, "{name} differs from CPython");
            }
            [Val::Result(Err(Some(error)))] => {
                ensure!(expected.is_null(), "{name} was rejected");
                let Val::Record(fields) = &**error else {
                    bail!("{name} returned the wrong error type");
                };
                let expected_message = case["error"].as_str();
                let expected_code = match case["code"].as_str() {
                    Some("Invalid") => "invalid",
                    Some("LimitExceeded") => "limit-exceeded",
                    _ => bail!("{name} has an unknown reference error code"),
                };
                ensure!(
                    matches!(fields.as_slice(), [(code, Val::Enum(kind)), (message, Val::String(text))]
                    if code.as_str() == "code" && kind.as_str() == expected_code
                        && message.as_str() == "message"
                        && expected_message.is_none_or(|expected| text.as_str() == expected)),
                    "{name} returned a different error from the reference"
                );
            }
            _ => bail!("{name} did not return a declared result"),
        }
        drop(result);
        drop(wasm);
        ensure!(
            account.snapshot()?.charged_bytes == 0,
            "{name} retained capacity"
        );
    }
    Ok(())
}
