//! Exercise the application presentation port in the packaged Wasm archive.

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

struct PresentationOwner;

impl NativeSetupLease for PresentationOwner {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"application presentation")
    }
}

#[tokio::test]
#[ignore = "requires the verified local application presentation archive"]
async fn application_presentation_uses_verified_wasm_port() -> Result<()> {
    let archive = std::env::var_os("TOBKIRI_APPLICATION_PRESENTATION_TPK")
        .context("set TOBKIRI_APPLICATION_PRESENTATION_TPK")?;
    let pack = verify_tpk(&fs::read(archive)?, &BTreeMap::new())?;
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(PresentationOwner)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let profile = ProfileId::parse("defaults")?;
    let shape = ShapeRef::parse("local.application-presentation.presentation@1.0")?;
    let operation = OpName::parse("read")?;
    let invoke = async |input: &str| -> Result<Option<Value>> {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let mut wasm = engine.native_setup_store(invocation, ())?;
        let port = wasm
            .prepare_declared_read_only_port(&pack, &shape, &operation, &profile, deadline, 0)
            .await?;
        let input = BudgetedString::copy_from(input, port.allocation_owners().component(), 1)?;
        let result = port
            .invoke_with_document_limit(&[Val::String(input)])
            .await?;
        let reply = match result.as_slice() {
            [Val::Result(Ok(Some(value)))] => {
                let Val::String(text) = &**value else {
                    anyhow::bail!("presentation port returned a different result type");
                };
                Some(serde_json::from_str(text.as_str())?)
            }
            [Val::Result(Err(Some(_)))] => None,
            _ => anyhow::bail!("presentation port did not return its declared result"),
        };
        drop(result);
        drop(wasm);
        ensure!(account.snapshot()?.charged_bytes == 0);
        Ok(reply)
    };

    let commands = serde_json::from_str(include_str!(
        "../../../packs/local.application-presentation-rust/data/commands.json"
    ))?;
    ensure!(invoke(r#"{"kind":"commands","profile_id":"defaults"}"#).await? == Some(commands));

    let mut ui: Value = serde_json::from_str(include_str!(
        "../../../packs/local.application-presentation-rust/data/ui.json"
    ))?;
    let options = json!([{"value":"org/model","label":"モデル"}]);
    let fields = ui["sections"]
        .as_array_mut()
        .and_then(|sections| {
            sections
                .iter_mut()
                .find(|section| section["id"] == "models")
        })
        .and_then(|section| section["fields"].as_array_mut())
        .context("original model display fields")?;
    for id in [
        "main_model",
        "lightweight_model",
        "preferred_model",
        "model_api_routes",
    ] {
        fields
            .iter_mut()
            .find(|field| field["id"] == id)
            .context("original model field")?["options"] = options.clone();
    }
    let request = json!({"kind":"ui","model_options":options,"profile_id":"defaults"});
    ensure!(invoke(&request.to_string()).await? == Some(ui));
    ensure!(
        invoke(r#"{"kind":"commands","model_options":[]}"#)
            .await?
            .is_none()
    );
    let too_large = json!({"kind":"ui","_session_id":"x".repeat(65_504)});
    ensure!(invoke(&too_large.to_string()).await?.is_none());
    Ok(())
}
