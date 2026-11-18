//! Exercise the migrated provider connection Registry with a source-built component.
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{path::Path, sync::Arc, time::Duration};
use support::{Host, canon, physical_temp, registry};
use tobkiri_core::{Core, CoreConfig, Session};

const PACK: &str = "local.provider-registry";
const RESOURCE: &str = "local.provider-registry.resource@1.0";
const MANAGE: &str = "local.provider-registry.manage@1.0";

async fn call(session: &Session, shape: &str, operation: &str, payload: Value) -> Result<Value> {
    let arguments = canon(&json!({"payload": payload}))?;
    let reply = tokio::time::timeout(
        Duration::from_secs(30),
        session.call_user_port(shape.parse()?, operation.parse()?, &arguments),
    )
    .await
    .context("provider Registry operation timed out")?
    .with_context(|| format!("provider Registry {operation} failed"))?;
    Ok(serde_json::from_slice(reply.as_bytes())?)
}

async fn snapshot(session: &Session, profile: &str) -> Result<Value> {
    call(session, RESOURCE, "snapshot", json!({"profile_id":profile})).await
}

async fn save(session: &Session, profile: &str, name: &str, revision: u64) -> Result<Value> {
    call(
        session,
        MANAGE,
        "apply",
        json!({
            "operation":"save", "profile_id":profile, "expected_revision":revision,
            "record":{
                "provider_instance_id":"conn-local", "adapter_id":"local-openai-compatible",
                "display_name":name, "endpoint":"http://127.0.0.1:24580/v1",
                "credential_handle":null, "enabled":true
            }
        }),
    )
    .await
}

#[tokio::test]
#[ignore = "requires a source-built provider Registry Wasm component"]
async fn provider_registry_uses_gated_private_storage_and_profile_revisions() -> Result<()> {
    let artifact = std::env::var_os("TOBKIRI_TEST_PROVIDER_REGISTRY_COMPONENT")
        .context("source-built provider Registry component required")?;
    let directory = physical_temp()?;
    let packs = [registry::pack(
        directory.path(),
        Path::new(&artifact),
        PACK,
        &["resource", "manage", "routing"],
        &[],
    )?];
    let a = registry::profile("prof_provider_a", &packs, &[RESOURCE, MANAGE])?;
    let b = registry::profile("prof_provider_b", &packs, &[RESOURCE, MANAGE])?;
    let host = Arc::new(Host::default());
    host.utility_ports.lock().unwrap().extend([
        (RESOURCE.into(), "snapshot".into()),
        (MANAGE.into(), "apply".into()),
    ]);
    host.approve_literal_user(&a)?;
    host.approve_literal_user(&b)?;
    let data = directory.path().join("data");
    let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host.clone())?;
    core.save_profile(a.clone(), None).await?;
    core.save_profile(b.clone(), None).await?;
    let sa = core.start(&a.id).await.context("start Profile A")?;
    let sb = match core.start(&b.id).await {
        Ok(session) => session,
        Err(error) => {
            sa.close().await?;
            return Err(error).context("start Profile B");
        }
    };
    let checked = async {
        ensure!(snapshot(&sa, a.id.as_str()).await?["revision"] == 0);
        save(&sa, a.id.as_str(), "Local A", 0).await?;
        let stale = save(&sa, a.id.as_str(), "Stale A", 0).await.unwrap_err();
        ensure!(
            format!("{stale:#}").contains("provider reported a conflict"),
            "{stale:#}"
        );
        let wrong_profile = save(&sa, b.id.as_str(), "Wrong Profile", 1)
            .await
            .unwrap_err();
        ensure!(
            format!("{wrong_profile:#}").contains("provider denied"),
            "{wrong_profile:#}"
        );
        ensure!(snapshot(&sb, b.id.as_str()).await?["revision"] == 0);
        save(&sb, b.id.as_str(), "Local B", 0).await?;
        for (session, profile, expected) in [(&sa, &a, "Local A"), (&sb, &b, "Local B")] {
            let result = snapshot(session, profile.id.as_str()).await?;
            ensure!(result["revision"] == 1);
            let provider = &result["providers"][0];
            ensure!(provider["display_name"] == expected);
            ensure!(provider["credential_status"] == "not_required");
            ensure!(provider.get("credential_handle").is_none());
            ensure!(provider.get("endpoint").is_none());
        }
        Ok(())
    }
    .await;
    let closed_a = sa.close().await;
    let closed_b = sb.close().await;
    checked?;
    closed_a.context("close Profile A")?;
    closed_b.context("close Profile B")?;
    drop((sa, sb, core));

    let core = Core::open(CoreConfig::for_test_data_root(data), host)?;
    let reopened = core.start(&a.id).await.context("restart Profile A")?;
    let persisted = snapshot(&reopened, a.id.as_str()).await;
    reopened.close().await?;
    let persisted = persisted?;
    ensure!(persisted["providers"][0]["display_name"] == "Local A");
    ensure!(persisted["revision"] == 1);
    Ok(())
}
