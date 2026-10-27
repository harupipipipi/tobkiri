//! Actual ordinary Packs, received UI scope, Profile Flow and existing Gate.
//! The UI host is a test attachment, not the original GUI or OS approval proof.
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs::OpenOptions, path::Path, sync::Arc};
use support::{Host, canon, parse_output, physical_temp};
use tobkiri_canon::{CanonValue, Digest, Variant, ids::*};
use tobkiri_core::{Core, CoreConfig, Session, UiAttachment};
use tobkiri_gate::{EffectRequest, LoopLimits, PermissionSet, ResourceLimits};
use tobkiri_manifest::{PackManifest, VerifiedPack};
use tobkiri_profile::{PackPin, Profile};

const PROJECTOR: &str = "local.harness-capability-projection-python";
const ENTRY: &str = "local.harness-capability-input";
const UI: &str = "local.capability-projection-ci-ui";
const PROJECTION: &str = "local.harness-capability-projection-python.projection@1.0";
const REQUESTS: &str = "local.harness-capability-input.requests@1.0";
const FLOW: &str = "local.harness-capability-input.projection-flow@1.0";
const GENERATION: u64 = 7;

fn literal_permissions(ports: &[&str]) -> PermissionSet {
    PermissionSet {
        ports: ports.iter().map(|s| (*s).to_owned()).collect(),
        wasm: ResourceLimits {
            fuel_per_call: 2_000_000_000,
            memory_mib: 64,
            wall_ms: 30_000,
            table_elements: 65536,
            output_bytes: 4 * 1024 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 16,
            max_wall_ms: 30_000,
            max_cost_units: 100_000,
        },
        ..Default::default()
    }
}

fn input(path: &Path, bound: u64) -> Result<Vec<u8>> {
    use std::io::Read;
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "physical CI input required"
    );
    let metadata = path.symlink_metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() <= bound,
        "CI input exceeds bound"
    );
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(bound + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 == metadata.len(), "CI input changed");
    Ok(bytes)
}

fn pack_pin(path: &Path, pack: &VerifiedPack) -> PackPin {
    PackPin {
        id: pack.manifest.id.clone(),
        version: pack.manifest.version.clone(),
        sha256: pack.hash.clone(),
        source: format!("file:{}", path.display()),
    }
}

fn ui_fixture(root: &Path, ceiling: &PermissionSet) -> Result<(PackPin, VerifiedPack)> {
    let path = root.join("ui-pack");
    std::fs::create_dir_all(path.join("ui"))?;
    let bytes = b"<!doctype html><title>CI attachment fixture</title>";
    std::fs::write(path.join("ui/index.html"), bytes)?;
    let mut permissions = ceiling.clone();
    permissions.ports = [REQUESTS.to_owned()].into_iter().collect();
    let manifest = json!({
        "format":"tobkiri.pack/1", "id":UI, "version":"0.1.0", "publisher_key":null,
        "runtime":{"kind":"data"}, "shapes":[],
        "ports":{"provides":[],"consumes":[{"shape":REQUESTS.rsplit_once('@').unwrap().0,
            "version":"1.0","optional":false}],"untyped":[]},
        "inputs":[],"flows":[],"overlays":[],"stores":[],"permissions":permissions,
        "ui":{"entry":"ui/index.html","ports_used":[REQUESTS]},
        "files":[{"path":"ui/index.html","sha256":Digest::hash(bytes),"size":bytes.len()}]
    });
    let typed: PackManifest = serde_json::from_value(manifest)?;
    std::fs::write(
        path.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&typed)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&path, &BTreeMap::new())?;
    Ok((pack_pin(&path, &verified), verified))
}

struct Fixture {
    _root: tempfile::TempDir,
    data: std::path::PathBuf,
    core: Core,
    host: Arc<Host>,
    profile: Profile,
    entry: VerifiedPack,
    projector: VerifiedPack,
}

impl Fixture {
    fn new(packs: &Path, allow_flow: bool) -> Result<Self> {
        let root = physical_temp()?;
        let entry_path = packs.join(ENTRY);
        let projector_path = packs.join(PROJECTOR);
        let entry = tobkiri_manifest::verify_directory(&entry_path, &BTreeMap::new())?;
        let projector = tobkiri_manifest::verify_directory(&projector_path, &BTreeMap::new())?;
        ensure!(
            entry.manifest.id.as_str() == ENTRY && projector.manifest.id.as_str() == PROJECTOR,
            "materialized ordinary Pack identity differs"
        );
        ensure!(
            entry.manifest.permissions == literal_permissions(&[PROJECTION, FLOW])
                && projector.manifest.permissions == literal_permissions(&[]),
            "ordinary Pack permissions or original limits differ from the literal review"
        );
        let mut ceiling = entry.manifest.permissions.clone();
        ceiling.ports.insert(REQUESTS.to_owned());
        let (ui_pin, ui) = ui_fixture(root.path(), &ceiling)?;
        let mut entry_limits = entry.manifest.permissions.clone();
        if !allow_flow {
            entry_limits.ports.remove(FLOW);
        }
        let limits = BTreeMap::from([
            (entry.manifest.id.clone(), entry_limits),
            (
                projector.manifest.id.clone(),
                projector.manifest.permissions.clone(),
            ),
            (ui.manifest.id.clone(), ui.manifest.permissions.clone()),
        ]);
        let profile: Profile = serde_json::from_value(json!({
            "format":"tobkiri.profile/1", "id":if allow_flow {"prof_capability_allowed"} else {"prof_capability_denied"},
            "name":"CI ordinary capability Flow", "publishers":{},
            "packs":[pack_pin(&entry_path,&entry),pack_pin(&projector_path,&projector),ui_pin],
            "ceiling":ceiling,"pack_limits":limits,"user_permissions":null,
            "bindings":{},
            "overlay_order":[],"user_overlays":[],"startup":{"inputs":[],"ui":UI}
        }))?;
        // Bind actual major-version views, not user-supplied provider identities.
        let mut profile = profile;
        profile.bindings = BTreeMap::from([
            (
                "local.harness-capability-input.requests@1".to_owned(),
                ENTRY.to_owned(),
            ),
            (
                "local.harness-capability-projection-python.projection@1".to_owned(),
                PROJECTOR.to_owned(),
            ),
        ]);
        let host = Arc::new(Host::default());
        *host.ui_enabled.lock().unwrap() = true;
        host.review_pack_permissions(&profile)?;
        let data = root.path().join("data");
        let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host.clone())?;
        Ok(Self {
            _root: root,
            data,
            core,
            host,
            profile,
            entry,
            projector,
        })
    }

    async fn start(&self) -> Result<(Session, UiAttachment)> {
        self.core.save_profile(self.profile.clone(), None).await?;
        let session = self.core.start(&self.profile.id).await?;
        session.bind_ui_generation(GENERATION)?;
        let mut ui = session.ui_attachments()?;
        ensure!(
            ui.len() == 1 && ui[0].pack.as_str() == UI,
            "actual UI attachment missing"
        );
        Ok((session, ui.remove(0)))
    }

    fn audit(&self) -> Result<Vec<Value>> {
        let path = self.data.join(tobkiri_store::DataRoot::profile_relative(
            &self.profile.id,
            "audit.sqlite",
        )?);
        let connection = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq LIMIT 1001")?;
        let mut cursor = statement.query([])?;
        let mut rows = Vec::new();
        while let Some(row) = cursor.next()? {
            let raw: Vec<u8> = row.get(0)?;
            ensure!(
                rows.len() < 1000 && raw.len() <= 65536,
                "bounded audit observation exceeded"
            );
            rows.push(serde_json::from_slice(&raw)?);
        }
        Ok(rows)
    }
}

async fn submit(
    session: &Session,
    ui: &UiAttachment,
    payload: &str,
) -> Result<tobkiri_canon::CoreBuffer> {
    Ok(session
        .ui_invoke_received(
            &ui.session,
            &ui.host_session,
            GENERATION,
            &ui.receiver,
            ShapeRef::parse(REQUESTS)?,
            OpName::parse("submit")?,
            &canon(&json!({"payload":payload}))?,
        )
        .await?)
}

fn has_pack_event(rows: &[Value], kind: &str, pack: &VerifiedPack) -> bool {
    rows.iter().any(|row| {
        row["kind"] == kind
            && row["principal"]["kind"] == "pack"
            && row["principal"]["pack"] == pack.manifest.id.as_str()
            && row["principal"]["hash"] == pack.hash.to_string()
    })
}

fn original_terminal_pair(rows: &[Value], entry: &VerifiedPack, payload: &str) -> Result<bool> {
    let effect = EffectRequest::PortCall {
        shape: ShapeRef::parse(PROJECTION)?,
        op: OpName::parse("project")?,
    }
    .digest()?
    .to_string();
    let args =
        tobkiri_canon::digest(&canon(&json!({"payload":payload}))?, Variant::StrictV1)?.to_string();
    let matching = |row: &&Value| {
        row["principal"]["kind"] == "pack"
            && row["principal"]["pack"] == entry.manifest.id.as_str()
            && row["principal"]["hash"] == entry.hash.to_string()
            && row["payload"]["effect_digest"] == effect
            && row["payload"]["args_digest"] == args
    };
    let starts: Vec<_> = rows
        .iter()
        .filter(matching)
        .filter(|row| row["kind"] == "dispatch")
        .collect();
    if starts.len() != 1 {
        return Ok(false);
    }
    Ok(rows
        .iter()
        .filter(matching)
        .filter(|row| {
            row["kind"] == "outcome"
                && row["request"] == starts[0]["request"]
                && row["payload"]["grant"] == starts[0]["payload"]["grant"]
                && row["payload"]["outcome"]["status"] == "succeeded"
        })
        .count()
        == 1)
}

#[tokio::test]
#[ignore = "external actual components and supported native Core, CI only"]
async fn ordinary_capability_ui_entry_runs_real_profile_flow_and_rejects_unapproved_route()
-> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && cfg!(all(target_os = "macos", target_arch = "aarch64")),
        "supported CI native platform required"
    );
    let materialized = std::path::PathBuf::from(std::env::var("TOBKIRI_CAPABILITY_PACKS")?);
    let case_path = std::path::PathBuf::from(std::env::var("TOBKIRI_CAPABILITY_CASES")?);
    let bytes = input(&case_path, 4 * 1024 * 1024)?;
    ensure!(
        Digest::hash(&bytes).as_str()
            == format!(
                "sha256:{}",
                std::env::var("TOBKIRI_CAPABILITY_CASES_SHA256")?
            ),
        "original cases changed"
    );
    let oracle: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        oracle["oracle_commit"] == "bf8cf14664580b7ca226b56d7bd5a311a19f44b4",
        "original oracle changed"
    );
    let cases: Vec<_> = oracle["cases"]
        .as_array()
        .context("original cases missing")?
        .iter()
        .filter(|case| case["expected"].is_string())
        .collect();
    ensure!(cases.len() == 15, "original factory cases missing");
    let fixture = Fixture::new(&materialized, true)?;
    let (session, ui) = fixture.start().await?;
    let mut observed = Vec::new();
    for case in &cases {
        let payload = case["payload"].as_str().context("case payload missing")?;
        let opened = submit(&session, &ui, payload).await?;
        let value = parse_output(&opened)?;
        ensure!(
            value.get("kind").and_then(CanonValue::as_str) == Some("stream"),
            "ordinary request did not return its actual Flow stream"
        );
        let handle = u64::try_from(
            value
                .get("handle")
                .and_then(CanonValue::as_i64)
                .context("Flow stream missing")?,
        )?;
        let mut completed = false;
        let mut ended = false;
        for _ in 0..64 {
            let buffer = session
                .ui_stream_pull_buffer(&ui.session, handle, 8)
                .await?;
            let frames = parse_output(&buffer)?;
            let frames = frames.as_array().context("Flow batch is not an array")?;
            if frames.is_empty() {
                ended = true;
                break;
            }
            for frame in frames {
                let kind = frame.get("kind").context("Flow event kind missing")?;
                match kind.get("kind").and_then(CanonValue::as_str) {
                    Some("completed") => {
                        ensure!(
                            !completed
                                && kind.get("value").and_then(CanonValue::as_str)
                                    == case["expected"].as_str(),
                            "ordinary Flow differs from original factory: {}",
                            case["name"]
                        );
                        completed = true;
                    }
                    Some("failed" | "cancelled") => anyhow::bail!("ordinary Flow failed: {kind:?}"),
                    _ => {}
                }
            }
        }
        ensure!(
            completed && ended,
            "ordinary Flow did not physically finish"
        );
        observed.push(json!({"name":case["name"],"equal":true}));
    }
    let before = fixture.audit()?;
    ensure!(
        has_pack_event(&before, "dispatch", &fixture.projector),
        "actual callee was not entered"
    );
    for case in &cases {
        ensure!(
            original_terminal_pair(&before, &fixture.entry, case["payload"].as_str().unwrap())?,
            "nested caller did not retain its original grant/request/arguments terminal"
        );
    }
    // UI declares only the request entry, never direct projection access.
    let result = session
        .ui_invoke_received(
            &ui.session,
            &ui.host_session,
            GENERATION,
            &ui.receiver,
            ShapeRef::parse(PROJECTION)?,
            OpName::parse("project")?,
            &canon(&json!({"payload":cases[0]["payload"]}))?,
        )
        .await;
    ensure!(result.is_err(), "UI bypassed the declared Flow entry");
    ensure!(
        fixture.host.user_approvals.lock().unwrap().is_empty()
            && fixture.host.utility_ports.lock().unwrap().is_empty(),
        "extra User document approval was introduced"
    );
    session.close().await?;
    let denied = Fixture::new(&materialized, false)?;
    let (session, ui) = denied.start().await?;
    ensure!(
        submit(&session, &ui, cases[0]["payload"].as_str().unwrap())
            .await
            .is_err(),
        "unapproved nested Flow route was accepted"
    );
    session.close().await?;
    let rows = denied.audit()?;
    ensure!(
        has_pack_event(&rows, "denied", &denied.entry)
            && !has_pack_event(&rows, "dispatch", &denied.projector),
        "denied route reached the callee or lost Gate denial"
    );
    let output = std::path::PathBuf::from(std::env::var("TOBKIRI_CAPABILITY_FLOW_REPORT")?);
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)?;
    serde_json::to_writer_pretty(
        file,
        &json!({
            "format":"tobkiri.capability-ordinary-flow/1","cases":observed,
            "actual_core_profile_flow_and_nested_pack_dispatch":true,"unapproved_nested_route_denied":true,
            "no_product_UserDocument_or_utility_api":true,"source_bound_pack_grants_checked":true,
            "ui_host_is_test_attachment":true,"original_full_GUI_connected":false,
            "actual_OS_approval_proof":false,"production_enabled":false,
        }),
    )?;
    Ok(())
}
