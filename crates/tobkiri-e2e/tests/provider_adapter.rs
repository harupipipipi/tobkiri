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
    let request_shape = ShapeRef::parse("local.provider-adapter.local-request@1.0")?;
    let request_operation = OpName::parse("compile")?;
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
        ensure!(
            document(&result)? == expected,
            "local Provider projection differs"
        );
        drop(result);
        drop(wasm);
        ensure!(account.snapshot()?.charged_bytes == 0);
    }
    let source = r#"{"model_id":"local/モデル","provider_id":"local","messages":[{"role":"user","content":"こんにちは"}],"parameters":{"max_tokens":16,"tools":[0],"temperature":-0.0},"tools":[{"id":"順番"}]}"#;
    let request = r#"{"model":"モデル","messages":[{"role":"user","content":"こんにちは"}],"stream":false,"max_tokens":16,"tools":[{"id":"順番"}],"temperature":-0.0}"#;
    for (streaming, expected) in [(false, generate), (true, stream)] {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let port = wasm
            .prepare_declared_read_only_pair(
                &pack,
                [(&request_shape, &request_operation), (&shape, &operation)],
                &profile,
                deadline,
            )
            .await?;
        let input = BudgetedString::copy_from(source, port.allocation_owners().component(), 1)?;
        let (compiled, next) = port
            .invoke_with_document_limit_then(&[Val::String(input)])
            .await?;
        ensure!(document(&compiled)? == request);
        drop(compiled);
        let input = BudgetedString::copy_from(original, next.allocation_owners().component(), 1)?;
        let response = next
            .invoke_with_document_limit(&[Val::String(input), Val::Bool(streaming)])
            .await?;
        ensure!(document(&response)? == expected);
        ensure!(wasm.fuel_remaining()? < pack.manifest.permissions.wasm.fuel_per_call);
        drop(response);
        drop(wasm);
        ensure!(account.snapshot()?.charged_bytes == 0);
    }
    let policy_operation = OpName::parse("compile-with-policy")?;
    let source = source.replace("モデル", "org/model");
    let request = request.replace("モデル", "org/model");
    for allowed in [true, false] {
        let policy = if allowed {
            r#"{"version":"tobkiri.connection-model-access.v1","mode":"explicit","model_ids":["org/model"]}"#
        } else {
            r#"{"version":"tobkiri.connection-model-access.v1","mode":"explicit","model_ids":[]}"#
        };
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let port = wasm
            .prepare_declared_read_only_pair(
                &pack,
                [(&request_shape, &policy_operation), (&shape, &operation)],
                &profile,
                deadline,
            )
            .await?;
        let owner = port.allocation_owners().component();
        let input = BudgetedString::copy_from(&source, owner, 1)?;
        let policy = BudgetedString::copy_from(policy, owner, 1)?;
        let (compiled, next) = port
            .invoke_with_document_limit_then(&[Val::String(input), Val::String(policy)])
            .await?;
        if allowed {
            ensure!(document(&compiled)? == request);
            drop(compiled);
            let input =
                BudgetedString::copy_from(original, next.allocation_owners().component(), 1)?;
            let response = next
                .invoke_with_document_limit(&[Val::String(input), Val::Bool(true)])
                .await?;
            ensure!(document(&response)? == stream);
        } else {
            let [Val::Result(Err(Some(error)))] = compiled.as_slice() else {
                anyhow::bail!("policy failed to deny the unlisted model");
            };
            let Val::Record(fields) = &**error else {
                anyhow::bail!("policy error had an unexpected type");
            };
            ensure!(
                matches!(fields.as_slice(), [(code, Val::Enum(kind)), (message, Val::String(text))]
                if code.as_str() == "code" && kind.as_str() == "denied"
                    && message.as_str() == "message"
                    && text.as_str() == "model is not permitted by this connection")
            );
            drop(compiled);
            drop(next);
        }
        drop(wasm);
        ensure!(account.snapshot()?.charged_bytes == 0);
    }
    Ok(())
}

fn document(output: &[Val]) -> Result<&str> {
    let [Val::Result(Ok(Some(value)))] = output else {
        anyhow::bail!("local Provider Adapter omitted its result");
    };
    let Val::String(text) = &**value else {
        anyhow::bail!("local Provider Adapter returned a different result type");
    };
    Ok(text.as_str())
}
