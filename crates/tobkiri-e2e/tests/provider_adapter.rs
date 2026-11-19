//! The ordinary saved local Provider response through the packaged Wasm port.

use anyhow::{Context, Result, ensure};
use std::{collections::BTreeMap, fs, sync::Arc};
use tobkiri_canon::{
    Digest,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_core::pack::verify_tpk;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};
use tobkiri_wasm::WasmEngine;
use wasmtime::component::{BudgetedString, Val};

struct ProjectionOwner;

impl NativeSetupLease for ProjectionOwner {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"local Provider Adapter response projection")
    }
}

#[tokio::test]
#[ignore = "requires the verified local Provider Adapter archive"]
async fn local_provider_response_through_original_core() -> Result<()> {
    let archive = std::env::var_os("TOBKIRI_PROVIDER_ADAPTER_TPK")
        .context("set TOBKIRI_PROVIDER_ADAPTER_TPK")?;
    let pack = verify_tpk(&fs::read(archive)?, &BTreeMap::new())?;
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(ProjectionOwner)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let shape = ShapeRef::parse("local.provider-adapter.local-completion@1.0")?;
    let operation = OpName::parse("project")?;
    let profile = ProfileId::parse("defaults")?;
    let original = r#"{"usage":{"completion_tokens":3,"prompt_tokens":4},"choices":[{"message":{"content":"こんにちは","tool_calls":[{"id":"local-tool"},42]},"finish_reason":"stop"}]}"#;
    let generate = r#"{"output":"こんにちは","tool_intents":[{"id":"local-tool"},42],"usage":{"completion_tokens":3,"prompt_tokens":4},"finish_reason":"stop"}"#;
    let stream = r#"{"output":"こんにちは","tool_intents":[{"id":"local-tool"},42],"usage":{"completion_tokens":3,"prompt_tokens":4},"finish_reason":"stop","events":[{"type":"text_delta","delta":"こんにちは"},{"type":"tool_intent_delta","tool_intent":{"id":"local-tool"}},{"type":"usage","usage":{"completion_tokens":3,"prompt_tokens":4}},{"type":"finish","finish_reason":"stop"}]}"#;
    let too_large = format!(
        r#"{{"choices":[{{"message":{{"content":"{}"}}}}]}}"#,
        "x".repeat(2 * 1024 * 1024)
    );
    for (response, streaming, expected) in [
        (original, false, generate),
        (original, true, stream),
        (r#"{"usage":{"total":NaN}}"#, false, ""),
        (r#"{"usage":"error"}"#, false, ""),
        (&too_large, true, ""),
    ] {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let text = BudgetedString::copy_from(response, wasm.allocation_owners().component(), 1)?;
        let port = wasm
            .prepare_declared_read_only_port(&pack, &shape, &operation, &profile, deadline, 0)
            .await?;
        let result = port
            .invoke_with_document_limit(&[Val::String(text), Val::Bool(streaming)])
            .await?;
        let [Val::Result(Ok(Some(value)))] = result.as_slice() else {
            anyhow::bail!("local Provider Adapter omitted its result");
        };
        let Val::String(text) = &**value else {
            anyhow::bail!("local Provider Adapter returned a different result type");
        };
        ensure!(
            text.as_str() == expected,
            "local Provider projection differs"
        );
        drop(result);
        drop(wasm);
        ensure!(account.snapshot()?.charged_bytes == 0);
    }
    Ok(())
}
