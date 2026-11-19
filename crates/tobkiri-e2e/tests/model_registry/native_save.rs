use super::{Context, Core, NativeArtifactError, Result, Source, Value, ensure, json};
use std::{
    cell::Cell,
    fs::{self, File, Permissions},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};
use tobkiri_core::{CoreValue, NativeRegistry};
use tobkiri_profile::PackPin;
use tobkiri_store::DataRoot;
use tobkiri_wasm::verify_legacy_model_save_declaration;

struct Save<'a> {
    core: Arc<Core>,
    root: &'a Path,
    data: PathBuf,
    pin: &'a PackPin,
    profile: &'a str,
    provider: Value,
}

impl Save<'_> {
    async fn stage(
        &self,
        request: &Value,
        expected_reads: usize,
    ) -> Result<(Value, NativeRegistry)> {
        let mut inspection = self
            .core
            .inspect_declared_artifacts(Source::new(self.root))?;
        let budget = Arc::clone(inspection.allocation_budget()?);
        let path = self
            .pin
            .source
            .strip_prefix("file:")
            .context("staged Model archive")?;
        let mut archive = File::open(path)?;
        let length = archive.metadata()?.len();
        let pack = inspection.verify_pack_archive(&mut archive, length)?;
        ensure!(pack.hash == self.pin.sha256);
        verify_legacy_model_save_declaration(&pack)?;
        let request = CoreValue::parse_guest(&serde_json::to_vec(request)?, &budget)?;
        let provider = CoreValue::parse_guest(&serde_json::to_vec(&self.provider)?, &budget)?;
        let registry = Arc::new(Mutex::new(
            inspection.legacy_model_registry(&self.data, self.profile)?,
        ));
        let reads = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&registry);
        let read_count = Arc::clone(&reads);
        let reply: Result<_> = async {
            let value = inspection
                .stage_live_legacy_model_save(
                    &pack,
                    self.profile,
                    &request,
                    &provider,
                    Box::new(move |budget| {
                        read_count.fetch_add(1, Ordering::Relaxed);
                        Ok(observed.lock().unwrap().observe_locked(budget, || Ok(()))?)
                    }),
                )
                .await?;
            Ok(value.encode_guest(&budget)?)
        }
        .await;
        inspection.finish().await?;
        ensure!(reads.load(Ordering::Relaxed) == expected_reads);
        let registry = Arc::into_inner(registry)
            .context("Wasm still retains the Registry")?
            .into_inner()
            .unwrap();
        let reply = reply?;
        Ok((serde_json::from_slice(reply.as_bytes())?, registry))
    }
}

fn rejected(error: &anyhow::Error, code: &str) -> Result<()> {
    ensure!(
        matches!(error.downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::ModelRegistryRejected(error)) if error.code() == code),
        "expected {code}, got {error:#}"
    );
    Ok(())
}

pub(super) async fn check_original_settings_save(
    core: Arc<Core>,
    root: &Path,
    pin: &PackPin,
) -> Result<()> {
    let oracle: Value = serde_json::from_str(include_str!(
        "../../../../packs/local.model-registry-rust/evidence/original-python-save.json"
    ))?;
    let case = |id: &str| -> Result<&Value> {
        oracle["cases"]
            .as_array()
            .context("original Model save cases missing")?
            .iter()
            .find(|case| case["id"] == id)
            .context("Model save case missing")
    };
    let create = case("create-first-route")?;
    let cap = case("save-updated-cap")?;
    let profile = create["profile_id"]
        .as_str()
        .context("original Model save Profile")?;
    let data = root.join("original-model-save-data");
    let legacy = DataRoot::open(&data)?;
    let relative =
        format!("packs/rumi_model_registry_pack/profiles/{profile}/model_registry.store.json");
    let model_file = data.join(&relative);
    let model_dir = model_file.parent().context("Model Registry directory")?;
    let provider_relative = format!(
        "packs/rumi_provider_registry_pack/profiles/{profile}/provider_registry.store.json"
    );
    let provider_bytes = b"{\"revision\":7}";
    legacy.write_atomic(&provider_relative, provider_bytes)?;
    let save = Save {
        core,
        root,
        data: data.clone(),
        pin,
        profile,
        provider: json!({"revision": 7, "providers":[{"provider_instance_id":"example-provider","enabled":true}]}),
    };

    let mut stale_provider = create["request"].clone();
    stale_provider["provider_registry_revision"] = json!(6);
    let status = save
        .stage(&stale_provider, 0)
        .await
        .err()
        .context("Provider revision was stale")?;
    rejected(&status, "denied")?;
    ensure!(
        !model_dir.exists(),
        "Provider rejection touched the Model Registry"
    );

    let (staged, txn) = save.stage(&create["request"], 1).await?;
    ensure!(staged["result"] == create["presented"]);
    let document = staged["document"]
        .as_str()
        .context("staged create document")?;
    ensure!(document.contains("\n  \"version\": ") && document.ends_with('\n'));
    ensure!(!model_file.exists());
    txn.publish(document.as_bytes(), || Ok(()))?;
    ensure!(fs::read(&model_file)? == document.as_bytes());

    let mut repeated = create["request"].clone();
    repeated["expected_revision"] = json!(1);
    let (noop, txn) = save.stage(&repeated, 1).await?;
    ensure!(noop["document"].is_null() && noop["result"] == create["presented"]);
    drop(txn);
    ensure!(fs::read(&model_file)? == document.as_bytes());
    let status = save
        .stage(&create["request"], 1)
        .await
        .err()
        .context("Model revision was stale")?;
    rejected(&status, "conflict")?;

    check_cap_update(&save, &legacy, &relative, &model_file, model_dir, cap).await?;
    ensure!(fs::read(data.join(provider_relative))? == provider_bytes);
    Ok(())
}

async fn check_cap_update(
    save: &Save<'_>,
    legacy: &DataRoot,
    relative: &str,
    model_file: &Path,
    model_dir: &Path,
    cap: &Value,
) -> Result<()> {
    let original = cap["document"].to_string();
    legacy.write_atomic(relative, original.as_bytes())?;
    fs::set_permissions(model_dir, Permissions::from_mode(0o755))?;
    let (staged, txn) = save.stage(&cap["request"], 1).await?;
    ensure!(staged["result"] == cap["presented"]);
    let replacement = staged["document"].as_str().context("staged cap document")?;
    let owner: Value = serde_json::from_str(
        staged["owner_result"]
            .as_str()
            .context("captured cap result")?,
    )?;
    let original_record = &cap["document"]["profiles"]["daily-model"];
    ensure!(owner["profile"]["credential_handle"] == original_record["credential_handle"]);
    ensure!(
        owner["profile"]["parameters"]["temperature"]
            == original_record["parameters"]["temperature"]
    );
    ensure!(owner["profile"]["parameters"]["nested"] == original_record["parameters"]["nested"]);
    ensure!(owner["profile"]["parameters"]["max_tokens"] == json!(131072));
    ensure!(
        staged["result"]["profiles"][0]
            .get("credential_handle")
            .is_none()
    );

    let guard_checked = Cell::new(false);
    let error = txn
        .publish(replacement.as_bytes(), || {
            if guard_checked.replace(true) {
                Err(NativeArtifactError::Denied)
            } else {
                Ok(())
            }
        })
        .expect_err("lost authorization must block rename");
    ensure!(matches!(error, NativeArtifactError::Denied));
    ensure!(fs::read(model_file)? == original.as_bytes());
    ensure!(fs::metadata(model_dir)?.permissions().mode() & 0o777 == 0o755);

    let (staged, txn) = save.stage(&cap["request"], 1).await?;
    let replacement = staged["document"]
        .as_str()
        .context("new staged cap document")?;
    txn.publish(replacement.as_bytes(), || Ok(()))?;
    ensure!(fs::read(model_file)? == replacement.as_bytes());
    ensure!(fs::metadata(model_dir)?.permissions().mode() & 0o777 == 0o700);
    ensure!(fs::metadata(model_file)?.permissions().mode() & 0o777 == 0o600);
    Ok(())
}
