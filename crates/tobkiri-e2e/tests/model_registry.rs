//! Exercise saved model Settings through the actual Core and Wasm Packs.
#[cfg(unix)]
#[path = "model_registry/native_save.rs"]
mod native_save;
#[path = "model_registry/registered_network.rs"]
mod registered_network;
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::sync::Arc;
#[cfg(unix)]
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
use support::registry::{PROVIDER_MANAGE, PROVIDER_PACK, RegistryPack};
use support::{Host, canon, physical_temp, registry, within_session};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
#[cfg(unix)]
use tobkiri_core::{
    CoreBuffer, NativeArtifactDeclaration, NativeArtifactError, NativeDeclaredArtifactSource,
};
use tobkiri_gate::StoreTier;
use tobkiri_store::{DataRoot, TieredStore};

const PACK: &str = "local.model-registry";
const SETTINGS: &str = "local.model-registry.settings@1.0";
const DATA: &str = "local.model-registry.data@1.0";
const STORE: &str = "model-registry";

fn packs() -> Result<[RegistryPack; 2]> {
    Ok([registry::pack(PACK)?, registry::pack(PROVIDER_PACK)?])
}

async fn call(session: &Session, shape: &str, operation: &str, arguments: Value) -> Result<Value> {
    let arguments = canon(&arguments)?;
    let shape_ref = shape.parse()?;
    let operation_ref = operation.parse()?;
    let output = within_session(session, operation, async {
        if shape == registry::PROVIDER_COMMIT {
            session
                .call_user_port_with_storage_errors(shape_ref, operation_ref, &arguments)
                .await
        } else {
            session
                .call_user_port(shape_ref, operation_ref, &arguments)
                .await
        }
    })
    .await
    .with_context(|| format!("{shape} {operation} failed"))?;
    Ok(serde_json::from_slice(output.as_bytes())?)
}

async fn list(session: &Session) -> Result<Value> {
    call(session, SETTINGS, "list-models", json!({})).await
}

#[tokio::test]
#[ignore = "requires staged Model and Provider Registry Pack archives"]
async fn existing_model_document_projects_settings_through_gate() -> Result<()> {
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
    let packs = packs()?;
    let pin = &packs[0].pin;
    let host = Arc::new(Host::default());
    host.utility_ports
        .lock()
        .unwrap()
        .insert((SETTINGS.into(), "list-models".into()));
    let data = root.path().join("data");
    let core = Arc::new(Core::open(
        CoreConfig::for_test_data_root(data.clone()),
        host.clone(),
    )?);
    for id in [original_profile, "prof_model_empty", "prof_model_foreign"] {
        let profile = registry::profile(id, &packs, &[SETTINGS])?;
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
    #[cfg(unix)]
    check_live_model_document(&core, root.path(), pin, original_profile, case).await?;
    Ok(())
}

#[cfg(unix)]
async fn check_live_model_document(
    core: &Arc<Core>,
    source_root: &std::path::Path,
    pin: &tobkiri_profile::PackPin,
    profile: &str,
    case: &Value,
) -> Result<()> {
    let data = source_root.join("original-user-data");
    let legacy = DataRoot::open(&data)?;
    let relative =
        format!("packs/rumi_model_registry_pack/profiles/{profile}/model_registry.store.json");
    let archive = pin
        .source
        .strip_prefix("file:")
        .context("staged Model archive")?;
    let run = || async {
        let mut inspection = core.inspect_declared_artifacts(Source::new(source_root))?;
        let budget = Arc::clone(inspection.allocation_budget()?);
        let mut archive = std::fs::File::open(archive)?;
        let length = archive.metadata()?.len();
        let pack = inspection.verify_pack_archive(&mut archive, length)?;
        ensure!(pack.hash == pin.sha256);
        let outcome: Result<CoreBuffer> = async {
            let output = inspection
                .list_legacy_model_settings(&pack, &data, profile)
                .await?;
            Ok(output.encode_guest(&budget)?)
        }
        .await;
        inspection.finish().await?;
        let output = outcome?;
        Ok::<Value, anyhow::Error>(serde_json::from_slice(output.as_bytes())?)
    };

    ensure!(run().await? == json!({"profiles":[],"count":0,"registry_revision":0}));
    ensure!(!data.join("packs").exists());
    let mut document = case["document"].clone();
    let before = document.to_string();
    legacy.write_atomic(&relative, before.as_bytes())?;
    ensure!(run().await? == case["result"]);
    ensure!(std::fs::read(data.join(&relative))? == before.as_bytes());

    let mut revised = case["result"].clone();
    let next_revision = document["revision"]
        .as_i64()
        .context("original Model revision")?
        + 1;
    document["revision"] = json!(next_revision);
    revised["registry_revision"] = json!(next_revision);
    legacy.write_atomic(&relative, document.to_string().as_bytes())?;
    ensure!(run().await? == revised);

    document["profile_id"] = json!("different_profile");
    legacy.write_atomic(&relative, document.to_string().as_bytes())?;
    ensure!(
        matches!(run().await.unwrap_err().downcast_ref::<NativeArtifactError>(),
        Some(NativeArtifactError::ModelRegistryRejected(error)) if error.code() == "denied")
    );
    legacy.write_atomic(&relative, b"null")?;
    ensure!(
        matches!(run().await.unwrap_err().downcast_ref::<NativeArtifactError>(),
        Some(NativeArtifactError::ModelRegistryRejected(error)) if error.code() == "invalid")
    );
    legacy.write_atomic(&relative, &vec![b' '; 1024 * 1024 + 1])?;
    ensure!(matches!(
        run()
            .await
            .unwrap_err()
            .downcast_ref::<NativeArtifactError>(),
        Some(NativeArtifactError::TooLarge)
    ));
    Ok(())
}

#[cfg(unix)]
struct Hash(String);

#[cfg(unix)]
impl NativeArtifactDeclaration for Hash {
    fn digest(&self) -> &str {
        &self.0
    }
}

#[cfg(unix)]
struct Source {
    root: PathBuf,
    declarations: BTreeMap<String, Hash>,
}

#[cfg(unix)]
impl Source {
    fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            declarations: BTreeMap::new(),
        }
    }
}

#[cfg(unix)]
impl NativeDeclaredArtifactSource for Source {
    type Declaration = Hash;
    fn root(&self) -> &Path {
        &self.root
    }
    fn declarations(&self) -> &BTreeMap<String, Hash> {
        &self.declarations
    }
}

async fn save_provider(session: &Session, profile: &str, name: &str, revision: u64) -> Result<()> {
    let saved = call(
        session,
        PROVIDER_MANAGE,
        "apply",
        json!({"payload": {
            "operation": "save",
            "profile_id": profile,
            "expected_revision": revision,
            "record": {
                "provider_instance_id": "conn-local",
                "adapter_id": "local-openai-compatible",
                "display_name": name,
                "endpoint": "http://127.0.0.1:24580/v1",
                "credential_handle": null,
                "enabled": true
            }
        }}),
    )
    .await?;
    ensure!(saved["action"] == "saved" && saved["store_revision"] == revision + 1);
    Ok(())
}

async fn save_model(session: &Session, payload: &Value) -> Result<Value> {
    call(session, SETTINGS, "save-model", json!({"payload": payload})).await
}

fn rejected(error: &anyhow::Error, expected_code: &str) -> Result<()> {
    ensure!(
        matches!(error.downcast_ref::<CoreError>(), Some(CoreError::Step(step)) if step.code == expected_code),
        "expected {expected_code}, got {error:#}"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires staged Model and Provider Registry Pack archives"]
async fn model_save_checks_both_registry_revisions_and_persists_the_token_limit() -> Result<()> {
    let root = physical_temp()?;
    let packs = packs()?;
    let profile = registry::profile("prof_model_save", &packs, &[SETTINGS, PROVIDER_MANAGE])?;
    let host = Arc::new(Host::default());
    host.utility_ports.lock().unwrap().extend([
        (SETTINGS.into(), "list-models".into()),
        (SETTINGS.into(), "save-model".into()),
        (PROVIDER_MANAGE.into(), "apply".into()),
    ]);
    host.approve_literal_user(&profile)?;
    let data = root.path().join("data");
    let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host.clone())?;
    core.save_profile(profile.clone(), None).await?;
    let session = core.start(&profile.id).await?;
    let saved = async {
        ensure!(list(&session).await? == json!({"profiles":[],"count":0,"registry_revision":0}));
        save_provider(&session, profile.id.as_str(), "Local", 0).await?;
        let mut request = json!({
            "model_profile_id": "local-chat",
            "model_id": "local/chat",
            "provider_instance_id": "conn-local",
            "display_name": "Local Chat",
            "expected_revision": 0,
            "provider_registry_revision": 1
        });
        let initial = json!({
            "profiles": [{
                "profile_id": "local-chat",
                "display_name": "Local Chat",
                "model_id": "local/chat",
                "provider_id": "conn-local",
                "route_configured": true
            }],
            "count": 1,
            "registry_revision": 1
        });
        ensure!(save_model(&session, &request).await? == initial);
        ensure!(list(&session).await? == initial);

        save_provider(&session, profile.id.as_str(), "Renamed connection", 1).await?;
        let mut stale_provider = request.clone();
        stale_provider["expected_revision"] = json!(1);
        stale_provider["max_output_tokens"] = json!(512);
        let error = save_model(&session, &stale_provider)
            .await
            .expect_err("save must reject the old provider revision");
        rejected(&error, "denied")?;
        ensure!(
            list(&session).await? == initial,
            "stale provider changed saved models"
        );

        request["provider_registry_revision"] = json!(2);
        let mut stale_model = request.clone();
        stale_model["max_output_tokens"] = json!(512);
        let error = save_model(&session, &stale_model)
            .await
            .expect_err("save must reject the old model revision");
        rejected(&error, "conflict")?;
        ensure!(
            list(&session).await? == initial,
            "stale model revision changed saved models"
        );

        request["expected_revision"] = json!(1);
        ensure!(save_model(&session, &request).await? == initial);
        ensure!(
            list(&session).await? == initial,
            "selector-only repeat changed the Registry"
        );

        request["max_output_tokens"] = json!(2048);
        let mut updated = initial;
        updated["profiles"][0]["max_output_tokens"] = json!(2048);
        updated["registry_revision"] = json!(2);
        ensure!(save_model(&session, &request).await? == updated);
        ensure!(list(&session).await? == updated);

        request["expected_revision"] = json!(2);
        request["max_output_tokens"] = json!(3072);
        let mut other = request.clone();
        other["max_output_tokens"] = json!(4096);
        let (left, right) =
            tokio::join!(save_model(&session, &request), save_model(&session, &other));
        let (written, rejected_save, cap) = match (left, right) {
            (Ok(written), Err(error)) => (written, error, 3072),
            (Err(error), Ok(written)) => (written, error, 4096),
            results => anyhow::bail!("expected a single successful update: {results:?}"),
        };
        rejected(&rejected_save, "conflict")?;
        updated["profiles"][0]["max_output_tokens"] = json!(cap);
        updated["registry_revision"] = json!(3);
        ensure!(written == updated);
        ensure!(list(&session).await? == updated);
        Ok::<_, anyhow::Error>(updated)
    }
    .await;
    let closed = session.close().await;
    let expected = saved?;
    closed?;
    drop((session, core));

    let core = Arc::new(Core::open(CoreConfig::for_test_data_root(data), host)?);
    let reopened = core.start(&profile.id).await?;
    let persisted = list(&reopened).await;
    reopened.close().await?;
    ensure!(
        persisted? == expected,
        "saved model configuration changed on restart"
    );
    #[cfg(unix)]
    native_save::check_original_settings_save(core, root.path(), &packs[0].pin).await?;
    Ok(())
}
