//! Replay the original guest DATA through the declared port and Core accounting.

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path, process::Command, sync::Arc};
use tobkiri_canon::{
    Digest,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_core::pack::{VerifiedPack, verify_tpk};
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupAccount, NativeSetupLease};
use tobkiri_wasm::WasmEngine;
use wasmtime::component::{BudgetedString, Val};

struct TestOwner;

impl NativeSetupLease for TestOwner {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"saved orchestration component test")
    }
}

#[tokio::test]
#[ignore = "requires the verified saved orchestration archive and original Python oracle"]
async fn original_saved_steps_through_core() -> Result<()> {
    let archive = std::env::var_os("TOBKIRI_SAVED_ORCHESTRATION_TPK")
        .context("set TOBKIRI_SAVED_ORCHESTRATION_TPK")?;
    let pack = verify_tpk(&fs::read(archive)?, &BTreeMap::new())?;
    let python = std::env::var_os("TOBKIRI_SAVED_ORCHESTRATION_ORACLE_PYTHON")
        .context("set TOBKIRI_SAVED_ORCHESTRATION_ORACLE_PYTHON")?;
    let runtime = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime");
    let reference = Command::new(python)
        .current_dir(runtime)
        .args([
            "-B",
            "-c",
            include_str!("../../../packs/local.saved-orchestration-rust/src/oracle_cases.py"),
        ])
        .output()?;
    ensure!(
        reference.status.success(),
        "reference guest failed: {}",
        String::from_utf8_lossy(&reference.stderr),
    );
    let cases: Vec<Value> = serde_json::from_slice(&reference.stdout)?;
    ensure!(cases.len() >= 40, "original oracle is incomplete");

    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(TestOwner)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    for case in cases {
        let request = serde_json::to_string(&case["input"])?;
        let expected = if case["ok"] == true {
            Some(serde_json::to_string(&case["output"])?)
        } else {
            None
        };
        let output = through_core(&engine, &account, &pack, &request)
            .await
            .with_context(|| format!("Wasm rejected {}", case["name"]))?;
        ensure!(
            output == expected,
            "Wasm differs from CPython for {}",
            case["name"]
        );
    }

    // Two maximum-sized inline images must fit in the real component and
    // caller's combined budget rather than a reduced fixture envelope.
    const IMAGE_BYTES: usize = 1024 * 1024;
    let payload = format!("R0lGODlh{}==", "A".repeat(IMAGE_BYTES.div_ceil(3) * 4 - 10));
    let raster =
        json!({"type":"image_url","image_url":{"url":format!("data:image/gif;base64,{payload}")}});
    let maximum = json!({"request":{
        "turn_id":"turn-limit","conversation_id":"conversation-limit","conversation_revision":1,
        "content":[{"type":"text","text":"日本語"}, raster, raster],
    }});
    let response = through_core(&engine, &account, &pack, &serde_json::to_string(&maximum)?)
        .await?
        .context("two maximum-sized inline rasters were rejected")?;
    let intent: Value = serde_json::from_str(&response)?;
    ensure!(intent["state"]["user_content"] == maximum["request"]["content"]);
    let read = json!({"state":intent["state"], "outcome":{"status":"ok","value":{
        "conversation":{"id":"conversation-limit", "conversation_revision":1,
            "messages":[], "model_reference":"model-limit"}
    }}});
    let next = through_core(&engine, &account, &pack, &serde_json::to_string(&read)?)
        .await?
        .context("two maximum-sized inline rasters were lost after the read")?;
    let append: Value = serde_json::from_str(&next)?;
    ensure!(append["state"]["user_content"].is_null());
    ensure!(append["payload"]["message"]["content"] == maximum["request"]["content"]);
    let ack = json!({"state":append["state"], "outcome":{"status":"ok","value":{
        "action":"message_appended","conversation_revision":2,"message":append["payload"]["message"]
    }}});
    let ai = through_core(&engine, &account, &pack, &serde_json::to_string(&ack)?)
        .await?
        .context("owner acknowledgement for two maximum-sized inline rasters was rejected")?;
    let ai: Value = serde_json::from_str(&ai)?;
    ensure!(ai["state"]["stage"] == "ai");

    // A saved tool trace with escaped arguments and results is already JSON.
    // Re-encoding it as a WIT string would overcount the admitted owner frame.
    let mut state = ai["state"].clone();
    state["stage"] = json!("tool");
    state["request"]["tool_selection"] = json!({"mode":"auto"});
    state["seen_tools"] = json!(["tool-1", "tool-2", "tool-3", "tool-4", "tool-5", "tool-6"]);
    state["tool_count"] = json!(5);
    let call = |index| {
        json!({
            "id":format!("tool-{index}"),"type":"function",
            "function":{"name":"fixture.echo","arguments":"{}"}
        })
    };
    let mut history = vec![json!({
        "role":"assistant","content":"","tool_calls":(1..=6).map(call).collect::<Vec<_>>()
    })];
    for index in 1..=5 {
        history.push(json!({
            "role":"tool","tool_call_id":format!("tool-{index}"),"content":"\\".repeat(200_000)
        }));
    }
    state["tool_messages"] = json!(history);
    state["pending_tools"] = json!([{
        "tool_id":"fixture.echo","tool_call_id":"tool-6","arguments":{},
        "expected_definition_hash":"b".repeat(64)
    }]);
    let tool_ack = json!({"state":state, "outcome":{"status":"ok","value":{
        "tool_id":"fixture.echo","tool_call_id":"tool-6","content":"\\".repeat(200_000)
    }}});
    let response = through_core(&engine, &account, &pack, &serde_json::to_string(&tool_ack)?)
        .await?
        .context("escape-heavy tool result was rejected")?;
    ensure!(response.len() <= tobkiri_canon::MAX_BYTES);
    ensure!(serde_json::to_string(&response)?.len() > tobkiri_canon::MAX_BYTES);
    let intent: Value = serde_json::from_str(&response)?;
    ensure!(intent["state"]["tool_count"] == 6);
    ensure!(intent["state"]["stage"] == "ai");
    Ok(())
}

async fn through_core(
    engine: &WasmEngine,
    account: &NativeSetupAccount,
    pack: &VerifiedPack,
    request: &str,
) -> Result<Option<String>> {
    let invocation = account.begin()?;
    let deadline = invocation.deadline()?;
    let mut wasm = engine.native_setup_store(invocation, ())?;
    let input = BudgetedString::copy_from(request, wasm.allocation_owners().component(), 1)?;
    let port = wasm
        .prepare_declared_read_only_port(
            pack,
            &ShapeRef::parse("local.saved-orchestration.legacy-step@1.0")?,
            &OpName::parse("advance")?,
            &ProfileId::parse("defaults")?,
            deadline,
            0,
        )
        .await?;
    let output = port
        .invoke_with_document_limit(&[Val::String(input)])
        .await?;
    let response = match output.as_slice() {
        [Val::Result(Ok(Some(value)))] => match &**value {
            Val::String(value) => Some(value.as_str().to_owned()),
            _ => anyhow::bail!("expected saved orchestration JSON"),
        },
        [Val::Result(Err(Some(value)))]
            if matches!(&**value, Val::Record(fields) if fields.iter().any(|(key, value)| {
                key.as_str() == "code" && matches!(value, Val::Enum(code) if code.as_str() == "invalid")
            })) =>
        {
            None
        }
        _ => anyhow::bail!("unexpected saved orchestration result"),
    };
    if request.len() > 2 * 1024 * 1024 {
        let used = pack.manifest.permissions.wasm.fuel_per_call - wasm.fuel_remaining()?;
        eprintln!("saved orchestration large step used {used} Wasm fuel");
    }
    drop(output);
    drop(wasm);
    ensure!(account.snapshot()?.charged_bytes == 0);
    Ok(response)
}
