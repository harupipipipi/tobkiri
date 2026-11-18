//! Exercise the migrated provider connection Registry with a source-built component.
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{path::Path, sync::Arc, time::Duration};
use support::registry::{
    PROVIDER_COMMIT, PROVIDER_CONFIGURATION as CONFIGURATION,
    PROVIDER_LEGACY_CONFIGURATION as LEGACY_CONFIGURATION,
};
use support::{Host, canon, physical_temp, registry, within_session};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
#[cfg(unix)]
use tobkiri_core::{
    CoreBuffer, CoreValue, LegacyProviderRegistryRead, NativeArtifactDeclaration,
    NativeArtifactError, NativeDeclaredArtifactSource,
};
use tobkiri_gate::{EffectRequest, StoreOperation, StoreTier};
use tobkiri_store::{DataRoot, StoreError};

const PACK: &str = "local.provider-registry";
const RESOURCE: &str = "local.provider-registry.resource@1.0";
const MANAGE: &str = "local.provider-registry.manage@1.0";
#[cfg(unix)]
const LEGACY_COMMIT: &str = "local.provider-registry.legacy-configuration-commit@1.0";

fn prepared_digest(plan: &Value) -> Result<String> {
    Ok(tobkiri_canon::digest(&canon(plan)?, tobkiri_canon::Variant::StrictV1)?.to_string())
}

async fn call(session: &Session, shape: &str, operation: &str, arguments: Value) -> Result<Value> {
    let arguments = canon(&arguments)?;
    let shape_ref = shape.parse()?;
    let operation_ref = operation.parse()?;
    let reply = tokio::time::timeout(Duration::from_secs(30), async {
        if shape == PROVIDER_COMMIT {
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
    .context("provider Registry operation timed out")?
    .with_context(|| format!("provider Registry {operation} failed"))?;
    Ok(serde_json::from_slice(reply.as_bytes())?)
}

async fn snapshot(session: &Session, profile: &str) -> Result<Value> {
    call(
        session,
        RESOURCE,
        "snapshot",
        json!({"payload":{"profile_id":profile}}),
    )
    .await
}

async fn save(session: &Session, profile: &str, name: &str, revision: u64) -> Result<Value> {
    call(
        session,
        MANAGE,
        "apply",
        json!({"payload":{
            "operation":"save", "profile_id":profile, "expected_revision":revision,
            "record":{
                "provider_instance_id":"conn-local", "adapter_id":"local-openai-compatible",
                "display_name":name, "endpoint":"http://127.0.0.1:24580/v1",
                "credential_handle":null, "enabled":true
            }
        }}),
    )
    .await
}

fn local_request(original: &Value) -> Value {
    json!({"request":{
        "connection-name":original["connection_name"],
        "endpoint":original["endpoint"],
        "catalog-provider-id":original["catalog_provider_id"],
        "display-name":original["display_name"],
        "capabilities":original["capabilities"]
    }})
}

async fn verify_local_preparation(session: &Session, profile: &str) -> Result<()> {
    let oracle: Value = serde_json::from_str(include_str!(
        "../../../packs/local.provider-registry-rust/evidence/configuration-prepare-python.json"
    ))?;
    let cases = oracle["cases"]
        .as_array()
        .context("missing prepare cases")?;
    for id in ["local", "local-slash", "local-ipv6", "local-nonloopback"] {
        let case = cases
            .iter()
            .find(|case| case["id"] == id)
            .with_context(|| format!("missing prepare case {id}"))?;
        let request = local_request(&case["request"]);
        let plan = call(session, CONFIGURATION, "prepare-local", request.clone()).await;
        if case.get("error").is_some() {
            let error = plan.expect_err("a non-loopback endpoint was prepared");
            ensure!(
                format!("{error:#}").contains("provider reported invalid input"),
                "{error:#}"
            );
            continue;
        }
        let mut expected = case["result"].clone();
        expected["profile_id"] = json!(profile);
        ensure!(plan? == expected, "configuration plan differs for {id}");

        if id == "local" {
            for extra in ["key-value", "profile-id"] {
                let mut invalid = request.clone();
                invalid["request"][extra] = json!("untrusted-selection");
                ensure!(
                    call(session, CONFIGURATION, "prepare-local", invalid)
                        .await
                        .is_err(),
                    "the typed request accepted {extra}"
                );
            }
        }
    }
    Ok(())
}

async fn verify_legacy_preparation(
    core: &Arc<Core>,
    pack: &tobkiri_profile::PackPin,
    session: &Session,
    unapproved: &Session,
    profile: &str,
    fixture: &Path,
) -> Result<()> {
    const HANDLE: &str = "credential:opaque-legacy-fixture";
    let original = json!({
        "connection_name":"authoritative", "endpoint":"http://127.0.0.1:1234/v1/"
    });
    let request = local_request(&original);
    let existing = json!({
        "version":"rumi.provider-registry.store.v1", "profile_id":profile, "revision":42,
        "migration":null,
        "providers":{"provider.cloud":{
            "provider_instance_id":"provider.cloud", "adapter_id":"openai-compatible",
            "credential_handle":HANDLE, "endpoint":"https://example.invalid/v1",
            "display_name":"Existing", "enabled":true,
            "record_revision":12
        }}
    });
    let legacy_path = fixture.join("old-product");
    std::fs::create_dir(&legacy_path)?;
    let source = DataRoot::open_existing(legacy_path)?;
    let path = format!(
        "packs/rumi_provider_registry_pack/profiles/{profile}/provider_registry.store.json"
    );
    let observe = {
        let source = source.clone();
        let path = path.clone();
        move || -> Result<Option<String>> {
            match source.read_existing_owned(&path, 1024 * 1024) {
                Ok(bytes) => Ok(Some(String::from_utf8(bytes)?)),
                Err(StoreError::NotFound) => Ok(None),
                Err(error) => Err(error.into()),
            }
        }
    };
    let arguments = |snapshot: Option<String>, preflight: Option<String>| json!({"request":request["request"],"snapshot":snapshot,"preflight":preflight});
    let prepare = |first, second| {
        call(
            session,
            LEGACY_CONFIGURATION,
            "prepare-local",
            arguments(first, second),
        )
    };
    let initial = prepare(observe()?, observe()?).await?;
    ensure!(initial["expected_revision"] == 0);
    #[cfg(unix)]
    ensure!(
        native_legacy_configuration(
            core,
            pack,
            profile,
            fixture,
            (&request["request"], None),
            observe.clone(),
            2
        )
        .await?
            == initial
    );
    ensure!(observe()?.is_none());

    let observed = existing.to_string();
    source.write_atomic(&path, observed.as_bytes())?;
    let verified = prepare(observe()?, observe()?).await?;
    let mut expected = call(session, CONFIGURATION, "prepare-local", request.clone()).await?;
    expected["expected_revision"] = json!(42);
    ensure!(verified == expected);
    #[cfg(unix)]
    {
        let plan = native_legacy_configuration(
            core,
            pack,
            profile,
            fixture,
            (&request["request"], None),
            observe.clone(),
            2,
        )
        .await?;
        ensure!(plan == expected);
        ensure!(!plan.to_string().contains(HANDLE));
        ensure!(observe()?.as_deref() == Some(observed.as_str()));

        let prepared = native_legacy_configuration(
            core,
            pack,
            profile,
            fixture,
            (&request["request"], Some(&plan)),
            observe.clone(),
            3,
        )
        .await?;
        ensure!(
            prepared
                == json!({
                    "expected_revision": 42,
                    "record": {
                        "provider_instance_id":"provider.authoritative",
                        "adapter_id":"local-openai-compatible",
                        "display_name":"provider.authoritative",
                        "credential_handle":null,
                        "endpoint":"http://127.0.0.1:1234/v1",
                        "enabled":true,
                        "data_residency":"unknown",
                        "health_evidence":{"status":"unknown","observed_at":null,"verified":false},
                        "metadata": {},
                        "record_revision":1
                    }
                })
        );
        ensure!(observe()?.as_deref() == Some(observed.as_str()));
        ensure!(
            call(
                session,
                LEGACY_COMMIT,
                "validate-local",
                json!({"request":request["request"],"plan":plan,"observe":{"id":1}}),
            )
            .await
            .is_err()
        );

        let mut edited = plan.clone();
        edited["request_digest"] = json!(format!("sha256:{}", "0".repeat(64)));
        let rejected = native_legacy_configuration(
            core,
            pack,
            profile,
            fixture,
            (&request["request"], Some(&edited)),
            observe.clone(),
            2,
        )
        .await
        .expect_err("native commit validation accepted an altered approved plan");
        ensure!(
            matches!(
                rejected.downcast_ref::<NativeArtifactError>(),
                Some(NativeArtifactError::ProviderRejected(error)) if error.code() == "denied"
            ),
            "{rejected:#}"
        );

        let mut newer = existing.clone();
        newer["revision"] = json!(43);
        let newer = newer.to_string();
        let race_source = source.clone();
        let race_path = path.clone();
        let race_document = newer.clone();
        let original_reader = observe.clone();
        let mut read_count = 0;
        let conflict = native_legacy_configuration(
            core,
            pack,
            profile,
            fixture,
            (&request["request"], Some(&plan)),
            move || {
                if read_count == 2 {
                    race_source.write_atomic(&race_path, race_document.as_bytes())?;
                }
                read_count += 1;
                original_reader()
            },
            3,
        )
        .await
        .expect_err("native commit validation accepted an intervening Registry save");
        ensure!(
            matches!(
                conflict.downcast_ref::<NativeArtifactError>(),
                Some(NativeArtifactError::ProviderRejected(error)) if error.code() == "conflict"
            ),
            "{conflict:#}"
        );
        ensure!(observe()?.as_deref() == Some(newer.as_str()));

        let mut invalid_request = request["request"].clone();
        invalid_request["endpoint"] = json!("https://example.invalid/v1");
        for (submitted, document, reads) in [
            (&invalid_request, observed.as_str(), 0),
            (&request["request"], "{", 1),
            (&request["request"], "null", 1),
        ] {
            source.write_atomic(&path, document.as_bytes())?;
            let error = native_legacy_configuration(
                core,
                pack,
                profile,
                fixture,
                (submitted, None),
                observe.clone(),
                reads,
            )
            .await
            .expect_err("native PREPARE accepted an invalid request or Registry document");
            ensure!(
                matches!(
                    error.downcast_ref::<NativeArtifactError>(),
                    Some(NativeArtifactError::ProviderRejected(error)) if error.code() == "invalid"
                ),
                "{error:#}"
            );
            ensure!(observe()?.as_deref() == Some(document));
        }
        source.write_atomic(&path, observed.as_bytes())?;
    }
    #[cfg(not(unix))]
    let _ = (core, pack);
    ensure!(!verified.to_string().contains(HANDLE));
    ensure!(observe()?.as_deref() == Some(observed.as_str()));
    ensure!(
        call(
            unapproved,
            LEGACY_CONFIGURATION,
            "prepare-local",
            arguments(observe()?, observe()?)
        )
        .await
        .is_err()
    );

    let mut raced = existing.clone();
    raced["revision"] = json!(43);
    let raced = raced.to_string();
    let before_replacement = observe()?;
    #[cfg(unix)]
    {
        let original_reader = observe.clone();
        let replacement_source = source.clone();
        let replacement_path = path.clone();
        let mut replacement = Some(raced.clone());
        let result = native_legacy_configuration(
            core,
            pack,
            profile,
            fixture,
            (&request["request"], None),
            move || {
                let observed = original_reader()?;
                // Replace the fixture between PREPARE's two observations.
                if let Some(document) = replacement.take() {
                    replacement_source.write_atomic(&replacement_path, document.as_bytes())?;
                }
                Ok(observed)
            },
            2,
        )
        .await
        .expect_err("native PREPARE must reject a changed Registry revision");
        ensure!(
            matches!(
                result.downcast_ref::<NativeArtifactError>(),
                Some(NativeArtifactError::ProviderRejected(error)) if error.code() == "conflict"
            ),
            "{result:#}"
        );
    }
    #[cfg(not(unix))]
    source.write_atomic(&path, raced.as_bytes())?;
    let after_replacement = observe()?;
    ensure!(after_replacement.as_deref() == Some(raced.as_str()));
    let conflict = prepare(before_replacement, after_replacement)
        .await
        .expect_err("preflight rejected a new Registry revision");
    ensure!(
        matches!(conflict.downcast_ref::<CoreError>(), Some(CoreError::Step(step))
        if step.code == "conflict")
    );
    let mut foreign = existing;
    foreign["profile_id"] = json!("prof_provider_b");
    ensure!(
        prepare(Some(observed), Some(foreign.to_string()))
            .await
            .is_err()
    );
    ensure!(prepare(Some("{".into()), None).await.is_err());
    Ok(())
}

#[cfg(unix)]
async fn native_legacy_configuration(
    core: &Arc<Core>,
    expected: &tobkiri_profile::PackPin,
    profile: &str,
    root: &Path,
    (request, frozen_plan): (&Value, Option<&Value>),
    mut observe: impl FnMut() -> Result<Option<String>> + Send + 'static,
    expected_reads: usize,
) -> Result<Value> {
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    enum NoDeclaration {}
    impl NativeArtifactDeclaration for NoDeclaration {
        fn digest(&self) -> &str {
            match *self {}
        }
    }

    struct OriginalSource {
        root: std::path::PathBuf,
        declarations: BTreeMap<String, NoDeclaration>,
    }
    impl NativeDeclaredArtifactSource for OriginalSource {
        type Declaration = NoDeclaration;

        fn root(&self) -> &Path {
            &self.root
        }

        fn declarations(&self) -> &BTreeMap<String, NoDeclaration> {
            &self.declarations
        }
    }

    let mut inspection = core.inspect_declared_artifacts(OriginalSource {
        root: root.to_path_buf(),
        declarations: BTreeMap::new(),
    })?;
    let reads = Arc::new(AtomicUsize::new(0));
    let read_count = Arc::clone(&reads);
    let outcome = async {
        let path = expected
            .source
            .strip_prefix("file:")
            .context("staged Provider archive")?;
        let mut file = std::fs::File::open(path)?;
        let length = file.metadata()?.len();
        let pack = inspection.verify_pack_archive(&mut file, length)?;
        ensure!(pack.hash == expected.sha256);
        let budget = inspection.allocation_budget()?.clone();
        let input = CoreValue::parse_guest(&serde_json::to_vec(request)?, &budget)?;
        let read: LegacyProviderRegistryRead = Box::new(move |budget| {
            read_count.fetch_add(1, Ordering::Relaxed);
            Ok(observe()?
                .map(|observation| CoreBuffer::copy_from(observation.as_bytes(), budget))
                .transpose()?)
        });
        let result = if let Some(plan) = frozen_plan {
            let plan = CoreValue::parse_guest(&serde_json::to_vec(plan)?, &budget)?;
            inspection
                .validate_live_legacy_provider_commit(&pack, profile, &input, &plan, read)
                .await?
        } else {
            inspection
                .invoke_live_legacy_provider_prepare(&pack, profile, &input, read)
                .await?
        };
        let output = result.encode_guest(&budget)?;
        Ok(serde_json::from_slice(output.as_bytes())?)
    }
    .await;
    inspection
        .finish()
        .await
        .context("finish native Provider configuration validation")?;
    let reads = reads.load(Ordering::Relaxed);
    ensure!(
        reads == expected_reads,
        "native Provider validation made {reads} Registry reads, expected {expected_reads}: {outcome:?}"
    );
    outcome
}

async fn verify_local_commit(
    sa: &Session,
    sb: &Session,
    host: &Host,
    profile_a: &str,
    profile_b: &str,
    request: Value,
    data: &Path,
) -> Result<()> {
    let before_a = snapshot(sa, profile_a).await?;
    let before_b = snapshot(sb, profile_b).await?;
    let plan_a = call(sa, CONFIGURATION, "prepare-local", request.clone()).await?;
    let plan_b = call(sb, CONFIGURATION, "prepare-local", request.clone()).await?;
    ensure!(plan_a["expected_revision"] == plan_b["expected_revision"]);
    let digest_a = prepared_digest(&plan_a)?;
    let digest_b = prepared_digest(&plan_b)?;
    let args_a = json!({"request":request["request"],"plan-digest":digest_a});
    let args_b = json!({"request":request["request"],"plan-digest":digest_b});
    ensure!(
        call(sb, PROVIDER_COMMIT, "execute-local", args_b.clone())
            .await
            .is_err()
    );
    ensure!(
        call(sa, PROVIDER_COMMIT, "execute-local", args_b)
            .await
            .is_err()
    );
    let mut edited = args_a.clone();
    edited["request"]["endpoint"] = json!("http://127.0.0.1:1234/v1/");
    ensure!(
        call(sa, PROVIDER_COMMIT, "execute-local", edited)
            .await
            .is_err()
    );

    let permission = (PROVIDER_COMMIT.into(), "execute-local".into());
    host.utility_ports.lock().unwrap().remove(&permission);
    let denied = call(sa, PROVIDER_COMMIT, "execute-local", args_a.clone()).await;
    host.utility_ports.lock().unwrap().insert(permission);
    ensure!(
        denied.is_err(),
        "the trusted prompt denied the exact commit"
    );
    ensure!(snapshot(sa, profile_a).await? == before_a);
    ensure!(snapshot(sb, profile_b).await? == before_b);

    verify_pending_storage_move(sa, data, profile_a, &args_a).await?;
    ensure!(snapshot(sa, profile_a).await? == before_a);

    let result = call(sa, PROVIDER_COMMIT, "execute-local", args_a.clone()).await?;
    ensure!(result == json!({"configured":true,"provider_instance_id":"provider.fixture"}));
    ensure!(
        call(sa, PROVIDER_COMMIT, "execute-local", args_a)
            .await
            .is_err()
    );
    let after = snapshot(sa, profile_a).await?;
    ensure!(after["revision"].as_u64() == before_a["revision"].as_u64().map(|r| r + 1));
    ensure!(snapshot(sb, profile_b).await? == before_b);
    Ok(())
}

async fn verify_pending_storage_move(
    session: &Session,
    data: &Path,
    profile: &str,
    arguments: &Value,
) -> Result<()> {
    let catalog = data.join("profiles").join(profile).join("data/stores.meta");
    let open_catalog = || {
        rusqlite::Connection::open_with_flags(&catalog, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE)
    };
    // The real Store fails closed when this isolated fixture's recovery
    // journal is damaged. The component receives its normal WIT error.
    open_catalog()?.execute(
        "INSERT INTO data_moves(id, phase, body) VALUES (?1, 'prepared', x'00')",
        ["0".repeat(64)],
    )?;
    let reported = call(session, PROVIDER_COMMIT, "execute-local", arguments.clone()).await;
    let arguments = canon(arguments)?;
    let ordinary = within_session(
        session,
        "ordinary commit with a damaged move journal",
        session.call_user_port(
            PROVIDER_COMMIT.parse()?,
            "execute-local".parse()?,
            &arguments,
        ),
    )
    .await;
    ensure!(open_catalog()?.execute("DELETE FROM data_moves WHERE id=?1", ["0".repeat(64)])? == 1);
    let reported = reported.expect_err("damaged move journal blocked the actual commit");
    ensure!(
        matches!(reported.downcast_ref::<CoreError>(), Some(CoreError::Step(step))
            if step.code == "storage-pending-data-move"
                && step.message == "storage-pending-data-move: storage move requires explicit repair; do not replay"),
        "{reported:#}"
    );
    ensure!(
        matches!(ordinary, Err(CoreError::Step(step)) if step.code == "internal"),
        "ordinary User calls keep the component's public error"
    );
    Ok(())
}

async fn verify_unknown_local_commit(
    session: &Session,
    data: &Path,
    profile: &tobkiri_canon::ids::ProfileId,
    previous_revision: u64,
) -> Result<()> {
    let request = local_request(&json!({
        "connection_name":"unknown", "endpoint":"http://127.0.0.1:1234/v1"
    }));
    let plan = call(session, CONFIGURATION, "prepare-local", request.clone()).await?;
    let arguments = json!({"request":request["request"],"plan-digest":prepared_digest(&plan)?});
    let effect = EffectRequest::Storage {
        profile: profile.clone(),
        owner: PACK.parse()?,
        store: "provider-registry".into(),
        tier: StoreTier::Private,
        operation: StoreOperation::SqlWrite,
    }
    .digest()?;
    let audit = data.join(DataRoot::profile_relative(profile, "audit.sqlite")?);
    let audit =
        rusqlite::Connection::open_with_flags(audit, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    // Reject only the audit delivery after this real, already seeded Registry
    // commits its conditional UPDATE. Admission and the SQLite write still run.
    audit.execute_batch(&format!(
        "CREATE TRIGGER registry_commit_outcome_fault BEFORE INSERT ON audit
         WHEN json_extract(CAST(NEW.body AS TEXT), '$.kind') = 'outcome'
          AND json_extract(CAST(NEW.body AS TEXT), '$.payload.outcome.status') = 'succeeded'
          AND json_extract(CAST(NEW.body AS TEXT), '$.payload.effect_digest') = '{effect}'
         BEGIN SELECT RAISE(FAIL, 'fixture SQL outcome delivery failure'); END;"
    ))?;
    let result = call(session, PROVIDER_COMMIT, "execute-local", arguments).await;
    audit.execute_batch("DROP TRIGGER registry_commit_outcome_fault;")?;

    let result = result.expect_err("acknowledgment failed after the SQLite commit");
    ensure!(
        matches!(result.downcast_ref::<CoreError>(), Some(CoreError::Step(step))
            if step.code == "storage-sql-commit-unknown"
                && step.message == "storage-sql-commit-unknown: operation may have committed; do not replay"),
        "{result:#}"
    );
    let path = data.join(DataRoot::profile_relative(
        profile,
        &format!("data/{PACK}/private.sqlite"),
    )?);
    let db =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let document: String = db.query_row(
        "SELECT document FROM provider_registry WHERE singleton = 1",
        [],
        |row| row.get(0),
    )?;
    let document: Value = serde_json::from_str(&document)?;
    ensure!(document["revision"] == previous_revision + 1);
    ensure!(
        document["providers"]["provider.unknown"]["provider_instance_id"] == "provider.unknown"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires a staged Provider Registry Pack archive"]
async fn provider_registry_uses_gated_private_storage_and_profile_revisions() -> Result<()> {
    let directory = physical_temp()?;
    let packs = [registry::pack(PACK)?];
    let a = registry::profile(
        "prof_provider_a",
        &packs,
        &[
            RESOURCE,
            MANAGE,
            CONFIGURATION,
            PROVIDER_COMMIT,
            LEGACY_CONFIGURATION,
        ],
    )?;
    let b = registry::profile(
        "prof_provider_b",
        &packs,
        &[RESOURCE, MANAGE, CONFIGURATION],
    )?;
    let host = Arc::new(Host::default());
    host.utility_ports.lock().unwrap().extend([
        (RESOURCE.into(), "snapshot".into()),
        (MANAGE.into(), "apply".into()),
        (CONFIGURATION.into(), "prepare-local".into()),
    ]);
    host.approve_literal_user(&b)?;
    host.utility_ports.lock().unwrap().extend([
        (PROVIDER_COMMIT.into(), "execute-local".into()),
        (LEGACY_CONFIGURATION.into(), "prepare-local".into()),
    ]);
    host.approve_literal_user(&a)?;
    let data = directory.path().join("data");
    let core = Arc::new(Core::open(
        CoreConfig::for_test_data_root(data.clone()),
        host.clone(),
    )?);
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
        let before = snapshot(&sa, a.id.as_str()).await?;
        ensure!(before["revision"] == 0);
        verify_local_preparation(&sa, a.id.as_str()).await?;
        verify_legacy_preparation(
            &core,
            &packs[0].pin,
            &sa,
            &sb,
            a.id.as_str(),
            directory.path(),
        )
        .await?;
        ensure!(snapshot(&sa, a.id.as_str()).await? == before);
        let local = local_request(&json!({
            "connection_name":"fixture", "endpoint":"http://127.0.0.1:1234/v1"
        }));
        save(&sa, a.id.as_str(), "Local A", 0).await?;
        let plan = call(&sa, CONFIGURATION, "prepare-local", local.clone()).await?;
        ensure!(plan["expected_revision"] == 1 && plan["profile_id"] == a.id.as_str());
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
        verify_local_commit(&sa, &sb, &host, a.id.as_str(), b.id.as_str(), local, &data).await?;
        Ok(())
    }
    .await;
    let closed_a = sa.close().await;
    let closed_b = sb.close().await;
    checked?;
    closed_a.context("close Profile A")?;
    closed_b.context("close Profile B")?;
    drop((sa, sb, core));

    let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host)?;
    let reopened = core.start(&a.id).await.context("restart Profile A")?;
    let checked = async {
        let persisted = snapshot(&reopened, a.id.as_str()).await?;
        ensure!(persisted["providers"][0]["display_name"] == "Local A");
        ensure!(persisted["revision"] == 2);
        ensure!(persisted["providers"][1]["provider_instance_id"] == "provider.fixture");
        verify_unknown_local_commit(&reopened, &data, &a.id, 2).await
    }
    .await;
    let closed = reopened.close().await;
    checked?;
    closed?;
    Ok(())
}
