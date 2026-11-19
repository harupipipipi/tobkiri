//! Replay the sealed Conversation guest through the actual read-only Wasm ports.

use anyhow::{Context, Result, ensure};
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

struct ConversationOwner;

impl NativeSetupLease for ConversationOwner {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"Conversation complete comparison")
    }
}

#[tokio::test]
#[ignore = "requires the compiled Conversation archive and sealed CPython reference"]
async fn conversation_complete_replays_original_guest() -> Result<()> {
    let archive = std::env::var_os("TOBKIRI_CONVERSATION_COMPLETE_TPK")
        .context("set TOBKIRI_CONVERSATION_COMPLETE_TPK")?;
    let reference = std::env::var_os("TOBKIRI_CONVERSATION_COMPLETE_REFERENCE")
        .context("set TOBKIRI_CONVERSATION_COMPLETE_REFERENCE")?;
    let cases: Vec<Value> = serde_json::from_slice(&fs::read(reference)?)?;
    ensure!(cases.len() == 60, "expected the complete reference suite");
    let pack = verify_tpk(&fs::read(archive)?, &BTreeMap::new())?;
    ensure!(pack.manifest.version == "0.1.0");
    ensure!(pack.manifest.id.as_str() == "local.conversation-complete");

    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(ConversationOwner)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let profile = ProfileId::parse("defaults")?;
    let shape = ShapeRef::parse("local.conversation-complete.data@1.0")?;

    for case in &cases {
        let name = case["name"].as_str().context("reference case name")?;
        let operation = OpName::parse(case["operation"].as_str().context("reference operation")?)?;
        let expected = case.get("output").context("reference output")?;
        let input = case.get("input").context("reference input")?.to_string();
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let port = wasm
            .prepare_declared_read_only_port(&pack, &shape, &operation, &profile, deadline, 0)
            .await
            .with_context(|| format!("prepare {operation}: {name}"))?;
        let input = BudgetedString::copy_from(&input, port.allocation_owners().component(), 1)?;
        let result = port
            .invoke_with_document_limit(&[Val::String(input)])
            .await
            .with_context(|| format!("execute {operation}: {name}"))?;
        match result.as_slice() {
            [Val::Result(Ok(Some(value)))] => {
                let Val::String(text) = &**value else {
                    anyhow::bail!("{operation}: {name} returned the wrong result type");
                };
                let actual: Value = serde_json::from_str(text.as_str())?;
                ensure!(
                    &actual == expected,
                    "{operation}: {name} differs from CPython"
                );
            }
            [Val::Result(Err(Some(_)))] => {
                ensure!(expected.is_null(), "{operation}: {name} was rejected");
            }
            _ => anyhow::bail!("{operation}: {name} did not return a declared result"),
        }
        drop(result);
        drop(wasm);
        ensure!(
            account.snapshot()?.charged_bytes == 0,
            "{operation}: {name} retained capacity"
        );
    }
    Ok(())
}
