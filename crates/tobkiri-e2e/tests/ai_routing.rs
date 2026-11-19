//! The original Gateway's pure DATA results through verified Wasm.

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

    let shape = ShapeRef::parse("local.ai-routing.usage-cost@1.0")?;
    let operation = OpName::parse("calculate")?;
    let cost = r#"{"usage":{"prompt_tokens":10,"completion_tokens":4},"pricing":{"input":0.125,"output":0.25,"currency":"USD"},"usage_provenance":"fixture-usage","pricing_revision":"fixture-catalog"}"#;
    let reply = invoke(&shape, &operation, cost).await?;
    ensure!(
        reply
            == r#"{"input_tokens":10.0,"output_tokens":4.0,"total_tokens":14.0,"cost":2.25,"currency":"USD","known":true,"usage_provenance":"fixture-usage","pricing_revision":"fixture-catalog"}"#
    );
    let unknown = r#"{"usage":{"input_tokens":null,"prompt_tokens":10,"completion_tokens":4},"pricing":{"input":0.125,"output":0.25}}"#;
    let reply = invoke(&shape, &operation, unknown).await?;
    ensure!(
        reply
            == r#"{"input_tokens":null,"output_tokens":4.0,"total_tokens":null,"cost":null,"currency":null,"known":false,"usage_provenance":"provider_reported","pricing_revision":null}"#
    );
    let outside_domain = r#"{"usage":{"input_tokens":1e308,"output_tokens":1e308},"pricing":{"input":1,"output":1}}"#;
    ensure!(invoke(&shape, &operation, outside_domain).await?.is_empty());

    let shape = ShapeRef::parse("local.ai-routing.tool-intents@1.0")?;
    let operation = OpName::parse("normalize")?;
    let tool_intents = r#"{"request_id":" fixture-tools ","intents":[{"function":{"name":" notes.lookup ","arguments":"{\"text\":\"こんにちは\",\"items\":[2,null]}"},"authority_granted":true,"approved":true,"executes":true},{"id":"fixture-note","name":"notes.describe","arguments":{}}]}"#;
    let reply = invoke(&shape, &operation, tool_intents).await?;
    let reply: Value = serde_json::from_str(&reply)?;
    ensure!(
        reply
            == json!({
                "intents": [
                    {
                        "intent_id": "fixture-tools:tool:0",
                        "request_id": "fixture-tools",
                        "operation": "notes.lookup",
                        "arguments": {"text": "こんにちは", "items": [2, null]},
                        "authority_granted": false,
                        "approved": false,
                        "approval_status": "unrequested",
                        "executes": false,
                    },
                    {
                        "intent_id": "fixture-note",
                        "request_id": "fixture-tools",
                        "operation": "notes.describe",
                        "arguments": {},
                        "authority_granted": false,
                        "approved": false,
                        "approval_status": "unrequested",
                        "executes": false,
                    },
                ]
            })
    );
    let invalid_arguments =
        r#"{"request_id":"fixture-tools","intents":[{"name":"notes.lookup","arguments":"[]"}]}"#;
    ensure!(
        invoke(&shape, &operation, invalid_arguments)
            .await?
            .is_empty()
    );

    let shape = ShapeRef::parse("local.ai-routing.request-prepare@1.0")?;
    let operation = OpName::parse("prepare")?;
    let prepare = r#"{"request_id":" fixture-prepare ","decision_time":10,"messages":[{"role":"user","content":"こんにちは"}],"parameters":{"max_tokens":24},"requirements":{"preferred_model_id":"org/local","unknown_present":true,"modalities":["text"]},"model_reference":"daily","resolve_only":true}"#;
    let reply: Value = serde_json::from_str(&invoke(&shape, &operation, prepare).await?)?;
    ensure!(
        reply
            == json!({
                "request_id": "fixture-prepare",
                "decision_time": 10.0,
                "deadline": 70.0,
                "messages": [{"role":"user","content":"こんにちは"}],
                "input": null,
                "parameters": {"max_tokens":24},
                "tools": [],
                "requirements": {"preferred_model_id":"org/local","modalities":["text"]},
                "credential_handle": null,
                "idempotency_key": null,
                "allow_failover": false,
                "policy_revision": "",
                "conversation_id": null,
                "profile_id": null,
                "model_profile_id": null,
                "model_reference": "daily",
            })
    );
    let expired = r#"{"request_id":"fixture-prepare","decision_time":10,"deadline":10}"#;
    ensure!(invoke(&shape, &operation, expired).await?.is_empty());
    let invalid_ref =
        r#"{"request_id":"fixture-prepare","decision_time":10,"credential_handle":"fixture"}"#;
    ensure!(invoke(&shape, &operation, invalid_ref).await?.is_empty());

    let shape = ShapeRef::parse("local.ai-routing.failover@1.0")?;
    let operation = OpName::parse("decide")?;
    let failover = r#"{"allow_failover":true,"idempotency_key":"fixture-turn","tools":[],"error_code":"provider_unavailable","attempt":1,"candidate_count":2,"deadline":20,"decision_time":10}"#;
    let reply: Value = serde_json::from_str(&invoke(&shape, &operation, failover).await?)?;
    ensure!(
        reply
            == json!({
                "allowed": true,
                "checks": {
                    "explicitly_allowed": true,
                    "idempotency_bound": true,
                    "tool_free": true,
                    "retryable_error": true,
                    "candidate_available": true,
                    "deadline_remaining": true,
                },
                "reason": "replay_safe_failover",
            })
    );
    let tool_bound = failover.replace(r#""tools":[]"#, r#""tools":[{"name":"notes.lookup"}]"#);
    let reply: Value = serde_json::from_str(&invoke(&shape, &operation, &tool_bound).await?)?;
    ensure!(reply["allowed"] == false);
    ensure!(reply["checks"]["tool_free"] == false);
    ensure!(reply["reason"] == "tool_free");

    let shape = ShapeRef::parse("local.ai-routing.gateway-result@1.0")?;
    let operation = OpName::parse("project")?;
    let candidate = json!({
        "model_id":"org/local", "provider_instance_id":"local-model",
        "catalog_provider_instance_id":"catalog", "catalog_revision":"r1",
    });
    let generate = json!({
        "role":"generate", "request_id":"fixture-result", "candidate":candidate,
        "value":{"output":"こんにちは", "usage":{"completion_tokens":2},
                 "finish_reason":"tool_calls", "task_state":"waiting_approval"},
    });
    let reply: Value =
        serde_json::from_str(&invoke(&shape, &operation, &generate.to_string()).await?)?;
    ensure!(reply["output"] == "こんにちは");
    ensure!(reply["task_state"] == "waiting_approval");
    ensure!(reply["usage_provenance"] == "provider_reported");
    ensure!(reply["pricing_revision"] == "r1");
    let mut rejected = generate;
    rejected["value"]["status"] = json!("failed");
    ensure!(
        invoke(&shape, &operation, &rejected.to_string())
            .await?
            .is_empty()
    );
    let stream = json!({
        "role":"stream", "request_id":"fixture-result", "candidate":candidate,
        "value":{"output":null, "delivery_mode":"incremental"},
        "tool_intents":[{"operation":"notes.lookup","approved":false,"executes":false}],
        "events":[{"type":"finish"}], "attempts":[],
    });
    let reply: Value =
        serde_json::from_str(&invoke(&shape, &operation, &stream.to_string()).await?)?;
    ensure!(reply["output"].is_null());
    ensure!(reply["delivery_mode"] == "incremental");
    ensure!(reply["tool_intents"] == stream["tool_intents"]);
    ensure!(reply["events"] == stream["events"]);
    Ok(())
}
