//! Exercise the saved model Settings projection through the actual Core.
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{path::Path, sync::Arc, time::Duration};
use support::{Host, canon, physical_temp, registry};
use tobkiri_core::{Core, CoreConfig, Session};
use tobkiri_gate::StoreTier;
use tobkiri_store::{DataRoot, TieredStore};

const PACK: &str = "local.model-registry";
const SETTINGS: &str = "local.model-registry.settings@1.0";
const DATA: &str = "local.model-registry.data@1.0";
const STORE: &str = "model-registry";

async fn list(session: &Session) -> Result<Value> {
    let arguments = canon(&json!({}))?;
    let output = tokio::time::timeout(
        Duration::from_secs(30),
        session.call_user_port(SETTINGS.parse()?, "list-models".parse()?, &arguments),
    )
    .await
    .context("model Settings operation timed out")??;
    Ok(serde_json::from_slice(output.as_bytes())?)
}

#[tokio::test]
#[ignore = "requires a source-built model Registry Wasm component"]
async fn existing_model_document_projects_settings_through_gate() -> Result<()> {
    let artifact = std::env::var_os("TOBKIRI_TEST_MODEL_REGISTRY_COMPONENT")
        .context("source-built model Registry component required")?;
    let oracle: Value = serde_json::from_str(include_str!(
        "../../../packs/local.model-registry-rust/evidence/original-python-read.json"
    ))?;
    let case = oracle["cases"]
        .as_array()
        .context("model Registry oracle cases missing")?
        .iter()
        .find(|case| case["id"] == "settings-saved-projection")
        .context("model Registry settings case missing")?;
    let original_profile = case["profile_id"].as_str().context("missing Profile ID")?;
    let root = physical_temp()?;
    let pin = registry::pack(root.path(), Path::new(&artifact), PACK, &["settings"])?;
    let host = Arc::new(Host::default());
    host.utility_ports
        .lock()
        .unwrap()
        .insert((SETTINGS.into(), "list-models".into()));
    let data = root.path().join("data");
    let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host.clone())?;
    for id in [original_profile, "prof_model_empty", "prof_model_foreign"] {
        let profile = registry::profile(id, &pin, &[SETTINGS])?;
        host.approve_literal_user(&profile)?;
        core.save_profile(profile.clone(), None).await?;
        if id != "prof_model_empty" {
            // Trusted test staging uses the original owner document as DATA;
            // the Wasm list can only read it through its granted private store.
            let stores = TieredStore::open(&DataRoot::open(&data)?, &profile.id)?;
            stores.register(&pin.id, STORE, StoreTier::Private, DATA)?;
            let stored = canon(&json!({"document":case["document"].to_string()}))?;
            stores.apply_seed(&pin.id, STORE, &pin.sha256, &stored)?;
        }
        let session = core.start(&profile.id).await?;
        let result = list(&session).await;
        session.close().await?;
        match id {
            "prof_model_empty" => {
                ensure!(result? == json!({"profiles":[],"count":0,"registry_revision":0}));
            }
            "prof_model_foreign" => {
                let error = result.unwrap_err();
                ensure!(
                    format!("{error:#}").contains("provider denied the operation"),
                    "{error:#}"
                );
            }
            _ => ensure!(result? == case["result"]),
        }
    }
    Ok(())
}
