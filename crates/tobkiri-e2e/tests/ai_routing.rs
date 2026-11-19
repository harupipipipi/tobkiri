//! The original Gateway's routing and stream results through verified Wasm.

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, sync::Arc};
use tobkiri_canon::{
    Digest,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_core::pack::verify_tpk;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};
use tobkiri_wasm::WasmEngine;
use wasmtime::component::{BudgetedString, Val};

struct RoutingOwner;

impl NativeSetupLease for RoutingOwner {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"original Gateway routing")
    }
}

#[tokio::test]
#[ignore = "requires the verified local AI Routing archive"]
async fn ai_routing_uses_verified_wasm_port() -> Result<()> {
    let archive =
        std::env::var_os("TOBKIRI_AI_ROUTING_TPK").context("set TOBKIRI_AI_ROUTING_TPK")?;
    let pack = verify_tpk(&fs::read(archive)?, &BTreeMap::new())?;
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(RoutingOwner)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let profile = ProfileId::parse("defaults")?;
    let shape = ShapeRef::parse("local.ai-routing.routing@1.0")?;
    let operation = OpName::parse("evaluate")?;
    let routed = json!({
        "models": [
            {"model_id":"org/古い", "provider_id":"vendor", "modalities":["text"],
             "health_provider_instance_id":"old", "priority":1},
            {"model_id":"org/モデル", "provider_id":"vendor", "modalities":["text"],
             "health_provider_instance_id":"current", "priority":9},
            {"model_id":"org/blocked", "provider_id":"ambiguous", "modalities":["text"]},
        ],
        "execution_providers": [
            {"provider_instance_id":"vendor-local", "routing_keys":["vendor"]},
            {"provider_instance_id":"any", "routing_keys":["*"]},
            {"provider_instance_id":"ambiguous-a", "routing_keys":["ambiguous"]},
            {"provider_instance_id":"ambiguous-b", "routing_keys":["ambiguous"]},
        ],
        "health": {
            "old":{"status":"unavailable", "observed_at":20},
            "current":{"status":"healthy", "observed_at":100},
        },
        "requirements":{"modalities":["text"], "health_max_age":60},
        "decision_time":100,
    });
    let invoke = async |shape: &ShapeRef, operation: &OpName, input: &str| -> Result<String> {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let port = wasm
            .prepare_declared_read_only_port(&pack, shape, operation, &profile, deadline, 0)
            .await?;
        let input = BudgetedString::copy_from(input, port.allocation_owners().component(), 1)?;
        let result = port
            .invoke_with_document_limit(&[Val::String(input)])
            .await?;
        let [Val::Result(Ok(Some(value)))] = result.as_slice() else {
            anyhow::bail!("Gateway DATA port omitted its result");
        };
        let Val::String(text) = &**value else {
            anyhow::bail!("Gateway DATA port returned a different result type");
        };
        let reply = text.as_str().to_owned();
        drop(result);
        drop(wasm);
        ensure!(account.snapshot()?.charged_bytes == 0);
        Ok(reply)
    };
    let reply = invoke(&shape, &operation, &routed.to_string()).await?;
    let reply: Value = serde_json::from_str(&reply)?;
    ensure!(reply["deterministic"] == true);
    ensure!(reply["selected"] == reply["candidates"][0]);
    ensure!(reply["selected"]["model_id"] == "org/モデル");
    ensure!(reply["selected"]["execution_provider_instance_id"] == "vendor-local");
    ensure!(reply["candidates"][1]["health"] == "unknown");
    ensure!(reply["excluded"][0]["reason"] == "execution_provider_ambiguous");
    ensure!(invoke(&shape, &operation, "{}").await?.is_empty());

    let shape = ShapeRef::parse("local.ai-routing.stream-result@1.0")?;
    let operation = OpName::parse("normalize")?;
    let stream = r#"{"request_id":" fixture-stream ","provider_attempt":2,"value":{"events":[{"type":"text_delta","delta":"こんにちは"},{"type":"tool_intent_delta","tool_intent":{"id":"fixture-tool"}},{"type":"usage","usage":{"completion_tokens":2}},{"type":"finish","finish_reason":"stop"}]}}"#;
    let reply = invoke(&shape, &operation, stream).await?;
    let reply: Value = serde_json::from_str(&reply)?;
    let events = reply["events"]
        .as_array()
        .context("normalized event list")?;
    ensure!(events.len() == 4);
    for (index, event) in events.iter().enumerate() {
        ensure!(event["request_id"] == "fixture-stream");
        ensure!(event["sequence"] == index);
        ensure!(event["provider_attempt"] == 2);
    }
    ensure!(events[0]["delta"] == "こんにちは");
    ensure!(events[1]["tool_intent"] == json!({"id": "fixture-tool"}));
    ensure!(events[2]["usage"] == json!({"completion_tokens": 2}));
    ensure!(events[3]["finish_reason"] == "stop");
    let after_finish = r#"{"request_id":"fixture-stream","value":[{"type":"finish"},{"type":"text_delta","delta":"late"}]}"#;
    ensure!(invoke(&shape, &operation, after_finish).await?.is_empty());
    Ok(())
}
