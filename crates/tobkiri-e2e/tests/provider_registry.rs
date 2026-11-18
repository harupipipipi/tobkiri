//! Exercise the migrated provider connection Registry with a source-built component.
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path, sync::Arc, time::Duration};
use support::{Host, canon, physical_temp, repo};
use tobkiri_canon::{Digest, ids::ShapeRef};
use tobkiri_core::{Core, CoreConfig, Session};
use tobkiri_gate::{LoopLimits, PermissionSet, ResourceLimits, StoragePermissions};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    StoreDefinition, StoreTier,
};
use tobkiri_profile::{PackPin, Profile};

const PACK: &str = "local.provider-registry";
const RESOURCE: &str = "local.provider-registry.resource@1.0";
const MANAGE: &str = "local.provider-registry.manage@1.0";
const DATA: &str = "local.provider-registry.data@1.0";

fn store_permissions() -> PermissionSet {
    PermissionSet {
        storage: StoragePermissions {
            private: true,
            ..Default::default()
        },
        wasm: ResourceLimits {
            fuel_per_call: 100_000_000,
            memory_mib: 32,
            wall_ms: 10_000,
            table_elements: 10_000,
            output_bytes: 1024 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 100,
            max_wall_ms: 10_000,
            max_cost_units: 10_000,
        },
        ..Default::default()
    }
}

fn pack(root: &Path, component: &Path) -> Result<PackPin> {
    let wit = fs::read(repo().join("packs/local.provider-registry-rust/wit/pack.wit"))?;
    let sidecar = br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec();
    let mut files = BTreeMap::from([
        ("component.wasm".to_owned(), fs::read(component)?),
        ("wit/pack.wit".to_owned(), wit),
        (
            "wit/deps/core.wit".to_owned(),
            include_bytes!("../../tobkiri-wit/wit/core-v1-2/core.wit").to_vec(),
        ),
    ]);
    let mut shapes = Vec::new();
    let mut provides = Vec::new();
    for (name, shape, kind) in [
        ("resource", RESOURCE, ShapeKind::Port),
        ("manage", MANAGE, ShapeKind::Port),
        ("data", DATA, ShapeKind::Data),
    ] {
        let shape = ShapeRef::parse(shape)?;
        files.insert(format!("wit/{name}.shape.json"), sidecar.clone());
        if kind == ShapeKind::Port {
            provides.push(PortDeclaration {
                shape: shape.name.clone(),
                version: shape.version,
                optional: false,
            });
        }
        shapes.push(ShapeDefinition {
            name: shape.name,
            version: shape.version,
            kind,
            wit: "wit/pack.wit".into(),
        });
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PACK.parse()?,
        version: "0.1.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "rust/1.94.0".into(),
            config: None,
        },
        shapes,
        ports: Ports {
            provides,
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![StoreDefinition {
            name: "provider-registry".into(),
            tier: StoreTier::Private,
            data_shape: DATA.parse()?,
            seed: None,
        }],
        permissions: store_permissions(),
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let directory = root.join(PACK);
    fs::create_dir_all(directory.join("wit/deps"))?;
    for (path, content) in files {
        fs::write(directory.join(path), content)?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    Ok(PackPin {
        id: manifest.id,
        version: manifest.version,
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.canonicalize()?.display()),
    })
}

fn profile(id: &str, pin: &PackPin) -> Result<Profile> {
    let pack_limits = store_permissions();
    let ceiling = PermissionSet {
        ports: [RESOURCE.into(), MANAGE.into()].into_iter().collect(),
        ..pack_limits.clone()
    };
    let user = PermissionSet {
        ports: ceiling.ports.clone(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1", "id":id, "name":"Provider Registry persistence fixture",
        "packs":[pin], "publishers":{}, "ceiling":ceiling, "user_permissions":user,
        "pack_limits": {(PACK):pack_limits},
        "bindings":{},
        "overlay_order":[], "user_overlays":[], "startup":{"inputs":[],"ui":null}
    }))?)
}

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
    let pack = pack(directory.path(), Path::new(&artifact))?;
    let a = profile("prof_provider_a", &pack)?;
    let b = profile("prof_provider_b", &pack)?;
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
