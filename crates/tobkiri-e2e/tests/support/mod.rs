//! Test-only host and actual Pack fixtures. No production test gate is enabled.

#![allow(dead_code)]
use anyhow::{Context, Result};
use async_trait::async_trait;
use std::{
    collections::{BTreeSet, VecDeque},
    future::Future,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tobkiri_canon::{
    CanonValue, CoreBuffer, CoreValue, Variant,
    ids::{InputSourceId, OpName, PackHash, PackId, ShapeRef, UiSessionId},
};
use tobkiri_core::{
    Core, CoreConfig, NativeInputCompletion, NativeInputEvent, NativeInputRouteStatus,
    NativeInputRunHandle, NativeInputStatus, Session,
};
use tobkiri_flow::{RunEvent, RunEventKind, StepError};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, EffectRequest, HostError, HostIo, TrustedPrompt, UiEmbed,
};
use tobkiri_profile::Profile;

pub fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .unwrap()
        .to_path_buf()
}
pub fn canon(value: &serde_json::Value) -> Result<CanonValue> {
    Ok(tobkiri_canon::parse(
        &serde_json::to_vec(value)?,
        Variant::StrictV1,
    )?)
}

/// Inspect an actual producer/transport buffer in the trusted test process.
/// The source buffer remains borrowed, with its allocation lease intact, for
/// the complete StrictV1 parse. This never constructs a guest CoreValue.
pub fn parse_output(buffer: &CoreBuffer) -> Result<CanonValue> {
    anyhow::ensure!(
        buffer.len() <= tobkiri_canon::MAX_BYTES,
        "test output exceeds its bound"
    );
    Ok(tobkiri_canon::parse(buffer.as_bytes(), Variant::StrictV1)?)
}

/// Check the public summary against the actual ordered native host envelope.
pub fn assert_native_completion(
    completion: &NativeInputCompletion,
    expected: NativeInputStatus,
    routes: &[NativeInputRouteStatus],
) -> Result<CanonValue> {
    anyhow::ensure!(
        completion.status == expected,
        "NativeInput status {:?}, expected {expected:?}; routes={:?}",
        completion.status,
        completion.routes,
    );
    assert_eq!(completion.routes.len(), routes.len());
    let encoded = parse_output(&completion.buffer)?;
    let status = match expected {
        NativeInputStatus::Succeeded => "succeeded",
        NativeInputStatus::Failed => "failed",
        NativeInputStatus::Cancelled => "cancelled",
    };
    assert_eq!(
        encoded.get("status").and_then(CanonValue::as_str),
        Some(status)
    );
    let encoded_routes = encoded
        .get("routes")
        .and_then(CanonValue::as_array)
        .context("native completion has no route array")?;
    assert_eq!(encoded_routes.len(), routes.len());
    for (index, ((actual, expected), encoded)) in completion
        .routes
        .iter()
        .zip(routes)
        .zip(encoded_routes)
        .enumerate()
    {
        assert_eq!(actual.route_index as usize, index);
        anyhow::ensure!(
            &actual.status == expected,
            "NativeInput route {index} status {:?}, expected {expected:?}; error_code={:?}; routes={:?}",
            actual.status,
            actual.error_code,
            completion.routes,
        );
        assert_eq!(
            encoded.get("index").and_then(CanonValue::as_i64),
            Some(index as i64)
        );
        let status = match expected {
            NativeInputRouteStatus::Succeeded => "succeeded",
            NativeInputRouteStatus::Denied => "denied",
            NativeInputRouteStatus::Failed => "failed",
            NativeInputRouteStatus::Cancelled => "cancelled",
            NativeInputRouteStatus::Unstarted => "unstarted",
            NativeInputRouteStatus::RetentionLimit => "retention-limit",
        };
        assert_eq!(
            encoded.get("status").and_then(CanonValue::as_str),
            Some(status)
        );
        assert!(
            encoded.get("result").is_some(),
            "route result field is absent"
        );
        match actual.error_code {
            Some(code) => assert_eq!(
                encoded.get("error").and_then(CanonValue::as_str),
                Some(code)
            ),
            None => assert_eq!(encoded.get("error"), Some(&CanonValue::Null)),
        }
    }
    Ok(encoded)
}

/// Report only closed error codes and trusted Flow step names, never payloads
/// or arbitrary provider error messages.
pub fn failure_events<'a>(events: impl IntoIterator<Item = &'a RunEvent>) -> Vec<String> {
    events
        .into_iter()
        .filter_map(|event| match &event.kind {
            RunEventKind::Failed { error } => Some(format!(
                "sequence={} Failed code={}",
                event.sequence,
                error.audit_code()
            )),
            RunEventKind::StepFailed {
                step,
                error,
                continued,
            } => Some(format!(
                "sequence={} StepFailed step={step:?} code={} continued={continued}",
                event.sequence,
                error.audit_code()
            )),
            _ => None,
        })
        .collect()
}

/// Bound one actual Session operation. On timeout, retain its pinned future
/// while requesting close on that same Session; never replay the operation.
pub async fn within_session<F: Future>(session: &Session, stage: &str, future: F) -> F::Output {
    let mut future = std::pin::pin!(future);
    match tokio::time::timeout(Duration::from_secs(30), &mut future).await {
        Ok(output) => output,
        Err(_) => {
            let close = tokio::time::timeout(Duration::from_secs(5), session.close()).await;
            let observation = match close {
                Ok(Ok(())) => "close returned success",
                Ok(Err(_)) => "close returned an error",
                Err(_) => "close still pending after 5s",
            };
            panic!(
                "{stage}: timed out after 30s; same Session close requested; {observation}; physical cleanup not established"
            );
        }
    }
}

async fn native_timeout(run: &mut NativeInputRunHandle, stage: &str) -> anyhow::Error {
    run.cancel.cancel();
    let observation = match tokio::time::timeout(Duration::from_secs(5), &mut run.result).await {
        Ok(Ok(completion)) => format!(
            "completion status={:?} routes={:?}",
            completion.status, completion.routes
        ),
        Ok(Err(error)) => format!("completion error_code={}", error.audit_code()),
        Err(_) => "completion still pending after 5s".into(),
    };
    anyhow::anyhow!(
        "{stage}: timed out; original run cancelled; same completion observed: {observation}; physical cleanup not established"
    )
}

/// An absolute deadline bounds the complete event stage, including a producer
/// that keeps sending progress without reaching its terminal state.
pub async fn next_native_event(
    run: &mut NativeInputRunHandle,
    stage: &str,
    deadline: tokio::time::Instant,
) -> Result<Option<NativeInputEvent>> {
    match tokio::time::timeout_at(deadline, run.events.recv()).await {
        Ok(event) => Ok(event),
        Err(_) => Err(native_timeout(run, stage).await),
    }
}

/// Await the original completion by mutable reference so a timeout can cancel
/// and observe that same run instead of dropping and replacing its future.
pub async fn native_completion(
    run: &mut NativeInputRunHandle,
    stage: &str,
) -> Result<NativeInputCompletion> {
    match native_result(run, stage).await? {
        Ok(completion) => Ok(completion),
        Err(error) => anyhow::bail!("{stage}: error_code={}", error.audit_code()),
    }
}

/// Inspect the actual terminal rejection without converting its stable code
/// into a test harness admission error.
pub async fn native_result(
    run: &mut NativeInputRunHandle,
    stage: &str,
) -> Result<Result<NativeInputCompletion, StepError>> {
    match tokio::time::timeout(Duration::from_secs(30), &mut run.result).await {
        Ok(result) => Ok(result),
        Err(_) => Err(native_timeout(run, stage).await),
    }
}

/// The trusted host scripts explicit user answers to Pack permission reviews.
/// Unexpected effect approvals are denied, not silently approved.
#[derive(Default)]
pub struct Host {
    pub lines: Mutex<VecDeque<String>>,
    pub output: Mutex<Vec<CoreBuffer>>,
    pub approvals: Mutex<Vec<String>>,
    reviewed_packs: Mutex<BTreeSet<(PackId, PackHash)>>,
    pub utility_ports: Mutex<BTreeSet<(String, String)>>,
    pub ui_enabled: Mutex<bool>,
    pub user_approvals: Mutex<BTreeSet<(String, String, u64)>>,
}
impl Host {
    /// Inspect retained terminal output only in the trusted test process.
    /// Each source keeps its actual producer allocation lease while borrowed.
    pub fn output_text(&self) -> Result<String> {
        let output = self.output.lock().unwrap();
        let mut text = String::new();
        for buffer in output.iter() {
            text.push_str(buffer.as_str()?);
        }
        Ok(text)
    }

    /// Script only the complete verified artifact set reviewed for this fixture.
    /// Replacing the set prevents an earlier private revision from remaining
    /// approved when a test deliberately repins its Pack.
    pub fn review_pack_permissions(
        &self,
        profile: &Profile,
    ) -> Result<Vec<tobkiri_manifest::VerifiedPack>> {
        let packs = profile
            .packs
            .iter()
            .map(|pin| {
                let path = pin
                    .source
                    .strip_prefix("file:")
                    .context("fixture review requires an explicit local Pack source")?;
                let pack =
                    tobkiri_manifest::verify_directory(Path::new(path), &profile.publishers)?;
                anyhow::ensure!(
                    pack.hash == pin.sha256
                        && pack.manifest.id == pin.id
                        && pack.manifest.version == pin.version,
                    "fixture Pack pin differs from artifact"
                );
                Ok(pack)
            })
            .collect::<Result<Vec<_>>>()?;
        *self.reviewed_packs.lock().unwrap() = packs
            .iter()
            .map(|pack| (pack.manifest.id.clone(), pack.hash.clone()))
            .collect();
        Ok(packs)
    }

    /// Approve one reviewed fresh fixture declaration, never borrowed Pack rights.
    pub fn approve_literal_user(&self, profile: &Profile) -> Result<()> {
        let declared = profile
            .user_permissions
            .as_ref()
            .context("explicit User declaration required")?;
        let expected = tobkiri_gate::PermissionSet {
            ports: self
                .utility_ports
                .lock()
                .unwrap()
                .iter()
                .map(|(shape, _)| shape.clone())
                .collect(),
            wasm: profile.ceiling.wasm.clone(),
            loops: profile.ceiling.loops.clone(),
            ..Default::default()
        };
        anyhow::ensure!(
            declared == &expected && declared.is_subset_of(&profile.ceiling),
            "fixture User declaration exceeds its literal utility-only policy"
        );
        let packs = self.review_pack_permissions(profile)?;
        let compiled = tobkiri_profile::compile(profile, &packs)
            .map_err(|conflicts| anyhow::anyhow!("fixture profile conflicts: {conflicts:?}"))?;
        let hash =
            tobkiri_canon::digest(&canon(&serde_json::to_value(profile)?)?, Variant::StrictV1)?;
        self.user_approvals.lock().unwrap().insert((
            hash.to_string(),
            compiled.plan_digest.to_string(),
            1,
        ));
        Ok(())
    }
}
#[async_trait]
impl TrustedPrompt for Host {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        match prompt.effect {
            EffectRequest::UserPermissions {
                profile_hash,
                revision,
                epoch,
            } if matches!(&prompt.principal, tobkiri_gate::Principal::User)
                && self.user_approvals.lock().unwrap().contains(&(
                    profile_hash.to_string(),
                    revision.to_string(),
                    epoch,
                )) =>
            {
                ApprovalAnswer::Approve
            }
            EffectRequest::Permissions { pack, hash }
                if matches!(&prompt.principal, tobkiri_gate::Principal::User)
                    && self
                        .reviewed_packs
                        .lock()
                        .unwrap()
                        .contains(&(pack.clone(), hash.clone())) =>
            {
                self.approvals.lock().unwrap().push(pack.to_string());
                ApprovalAnswer::Approve
            }
            EffectRequest::PortCall { shape, op }
                if matches!(&prompt.principal, tobkiri_gate::Principal::User)
                    && self
                        .utility_ports
                        .lock()
                        .unwrap()
                        .contains(&(shape.to_string(), op.to_string())) =>
            {
                ApprovalAnswer::Approve
            }
            _ => ApprovalAnswer::Deny,
        }
    }
}

pub fn physical_temp() -> Result<tempfile::TempDir> {
    let directory = repo().parent().unwrap().join(".work/e2e-temp");
    std::fs::create_dir_all(&directory)?;
    Ok(tempfile::tempdir_in(directory.canonicalize()?)?)
}
#[async_trait]
impl HostIo for Host {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line_budgeted(
        &self,
        _: &str,
        max_bytes: usize,
        budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<CoreBuffer, HostError> {
        // Allocate the bounded native read destination from the supplied owner
        // before observing or consuming the scripted physical input. The
        // finished buffer retains that same storage and its allocation lease.
        let mut buffer = CoreBuffer::builder(&budget, max_bytes);
        buffer
            .write_chunk(max_bytes, |destination| {
                let mut lines = self.lines.lock().unwrap();
                let line = lines
                    .front()
                    .ok_or(tobkiri_canon::AllocationError::Invalid)?;
                if line.len() > destination.len() {
                    return Err(tobkiri_canon::AllocationError::Limit);
                }
                let length = line.len();
                destination[..length].copy_from_slice(line.as_bytes());
                lines.pop_front();
                Ok(length)
            })
            .map_err(|error| HostError(error.to_string()))?;
        buffer
            .finish()
            .map_err(|error| HostError(error.to_string()))
    }
    fn terminal_write_budgeted(&self, text: &CoreBuffer) -> Result<(), HostError> {
        text.as_str()
            .map_err(|error| HostError(error.to_string()))?;
        self.output.lock().unwrap().push(text.share());
        Ok(())
    }
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        Err(HostError(
            "scripted guest input requires its allocation owner".into(),
        ))
    }
    fn terminal_write(&self, _: &str) {
        panic!("scripted guest output requires its allocation owner");
    }
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        if *self.ui_enabled.lock().unwrap() {
            UiSessionId::new("ui_test_host").map_err(|error| HostError(error.to_string()))
        } else {
            Err(HostError("no UI in terminal fixture".into()))
        }
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        None
    }
}

/// A private fixture copies only the manifest-admitted Pack files, never legacy data.
pub struct Fixture {
    pub temp: tempfile::TempDir,
    pub profile: Profile,
    pub host: Arc<Host>,
    pub shape: ShapeRef,
    pub input: InputSourceId,
    pub input_operation: OpName,
}
impl Fixture {
    pub fn new(name: &str, origin: Option<&str>) -> Result<Self> {
        let root = repo();
        let document = root
            .join("packs/profiles")
            .join(format!("{name}.profile.json"));
        anyhow::ensure!(
            document.exists(),
            "Pack fixtures are absent; run python3 -B packs/build.py first"
        );
        let mut value: serde_json::Value = serde_json::from_slice(&std::fs::read(document)?)?;
        let temporary_root = root.parent().unwrap().join(".work/e2e-temp");
        std::fs::create_dir_all(&temporary_root)?;
        let temp = tempfile::tempdir_in(temporary_root.canonicalize()?)?;
        for pin in value["packs"]
            .as_array_mut()
            .context("Profile packs missing")?
        {
            let id = pin["id"].as_str().unwrap();
            let source = root.join("packs").join(id);
            let target = temp.path().join("packs").join(id);
            let mut manifest: serde_json::Value =
                serde_json::from_slice(&std::fs::read(source.join("pack.json"))?)?;
            for file in manifest["files"].as_array().unwrap() {
                let path = file["path"].as_str().unwrap();
                let destination = target.join(path);
                std::fs::create_dir_all(destination.parent().unwrap())?;
                std::fs::copy(source.join(path), destination).with_context(|| {
                    format!("fixture artifact missing: {id}/{path}; run packs/build.py")
                })?;
            }
            if let Some(origin) = origin.filter(|_| id == "local.openai-compat-provider") {
                manifest["permissions"]["network"]["origins"] = serde_json::json!([origin]);
                let config_path = manifest["runtime"]["config"]["path"]
                    .as_str()
                    .unwrap()
                    .to_string();
                let mut config: serde_json::Value =
                    serde_json::from_slice(&std::fs::read(target.join(&config_path))?)?;
                config["base_url"] = serde_json::json!(format!("{origin}/v1"));
                config["secret_name"] = serde_json::Value::Null;
                let bytes = serde_json::to_vec_pretty(&config)?;
                std::fs::write(target.join(&config_path), &bytes)?;
                for file in manifest["files"].as_array_mut().unwrap() {
                    if file["path"] == config_path {
                        file["sha256"] =
                            serde_json::json!(tobkiri_canon::Digest::hash(&bytes).to_string());
                        file["size"] = serde_json::json!(bytes.len());
                    }
                }
            }
            let manifest_value = canon(&manifest)?;
            pin["sha256"] = serde_json::json!(
                tobkiri_canon::digest(&manifest_value, Variant::StrictV1)?.to_string()
            );
            pin["source"] = serde_json::json!(format!("file:{}", target.display()));
            std::fs::write(
                target.join("pack.json"),
                serde_json::to_vec_pretty(&manifest)?,
            )?;
        }
        if let Some(origin) = origin {
            value["ceiling"]["network"]["origins"] = serde_json::json!([origin]);
            *value
                .pointer_mut("/pack_limits/local.openai-compat-provider/network/origins")
                .context("external provider fixture requires explicit network limits")? =
                serde_json::json!([origin]);
        }
        let profile: Profile = serde_json::from_value(value)?;
        let [entry] = profile.startup.inputs.as_slice() else {
            anyhow::bail!("this fixture requires exactly one explicit Input entry");
        };
        let input = InputSourceId::parse(entry.source_pack.as_str())?;
        let shape = entry.exact_input_view.clone();
        let input_operation = entry.operation.clone();
        Ok(Self {
            temp,
            profile,
            host: Arc::new(Host::default()),
            shape,
            input,
            input_operation,
        })
    }
    pub fn data_root(&self) -> PathBuf {
        self.temp.path().join("data")
    }

    /// Change one existing, admitted JSON file in this private fixture, then
    /// verify and repin the resulting Pack through the normal manifest path.
    pub fn replace_pack_json(
        &mut self,
        pack: &str,
        relative: &str,
        value: &serde_json::Value,
    ) -> Result<()> {
        let pin = self
            .profile
            .packs
            .iter_mut()
            .find(|pin| pin.id.as_str() == pack)
            .context("fixture Pack is not selected")?;
        let source = Path::new(
            pin.source
                .strip_prefix("file:")
                .context("fixture Pack is not local")?,
        )
        .canonicalize()?;
        anyhow::ensure!(
            source.starts_with(self.temp.path().canonicalize()?),
            "fixture mutation must remain inside its private directory"
        );
        let previous = tobkiri_manifest::verify_directory(&source, &Default::default())?;
        anyhow::ensure!(previous.hash == pin.sha256, "fixture Pack pin was stale");
        let mut manifest: serde_json::Value =
            serde_json::from_slice(&std::fs::read(source.join("pack.json"))?)?;
        let file = manifest["files"]
            .as_array_mut()
            .context("fixture Pack files missing")?
            .iter_mut()
            .find(|file| file["path"].as_str() == Some(relative))
            .context("fixture mutation must name an admitted file")?;
        let bytes = serde_json::to_vec_pretty(value)?;
        anyhow::ensure!(
            bytes.len() <= 2 * 1024 * 1024,
            "fixture JSON exceeds its bound"
        );
        std::fs::write(source.join(relative), &bytes)?;
        file["sha256"] = serde_json::json!(tobkiri_canon::Digest::hash(&bytes).to_string());
        file["size"] = serde_json::json!(bytes.len());
        std::fs::write(
            source.join("pack.json"),
            serde_json::to_vec_pretty(&manifest)?,
        )?;
        let verified = tobkiri_manifest::verify_directory(&source, &Default::default())?;
        anyhow::ensure!(
            verified.manifest.id == pin.id && verified.manifest.version == pin.version,
            "private fixture changed its Pack identity"
        );
        pin.sha256 = verified.hash;
        Ok(())
    }

    pub fn core(&self) -> Result<Core> {
        self.host.review_pack_permissions(&self.profile)?;
        let config = CoreConfig::for_test_data_root(self.data_root());
        Ok(Core::open(config, self.host.clone())?)
    }
    pub async fn start(&self) -> Result<(Core, Session)> {
        let core = self.core()?;
        tokio::time::timeout(
            Duration::from_secs(30),
            core.save_profile(self.profile.clone(), None),
        )
        .await
        .context("fixture startup: save Profile timed out; no Session exists yet")??;
        let session = tokio::time::timeout(Duration::from_secs(30), core.start(&self.profile.id))
            .await
            .context("fixture startup: start Session timed out; no Session handle returned")??;
        Ok((core, session))
    }
    pub async fn turn(&self, session: &Session, text: &str) -> Result<Vec<RunEvent>> {
        self.host.lines.lock().unwrap().push_back(text.into());
        let input = within_session(
            session,
            "turn: physical Input read",
            session.read_input(
                self.input.clone(),
                self.shape.clone(),
                self.input_operation.clone(),
            ),
        )
        .await
        .context("read input component")?;
        let mut run = within_session(
            session,
            "turn: NativeInput admission",
            session.submit_native_input(input),
        )
        .await
        .context("submit input to Flow")?;
        anyhow::ensure!(
            run.routes().len() == 1,
            "turn fixture requires one frozen route"
        );
        let plan_digest = run.routes()[0].target.plan_digest.clone();
        let mut events = Vec::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        while let Some(tagged) =
            next_native_event(&mut run, "turn: Flow progress", deadline).await?
        {
            assert_eq!(tagged.route_index, 0, "event left the frozen route");
            assert_eq!(tagged.event.plan_digest, plan_digest);
            events.push(tagged.event);
        }
        let completion = native_completion(&mut run, "turn: NativeInput completion")
            .await
            .with_context(|| format!("turn: failure_events={:?}", failure_events(&events)))?;
        assert_native_completion(
            &completion,
            NativeInputStatus::Succeeded,
            &[NativeInputRouteStatus::Succeeded],
        )
        .with_context(|| format!("turn: failure_events={:?}", failure_events(&events)))?;
        anyhow::ensure!(
            events
                .iter()
                .any(|e| matches!(e.kind, RunEventKind::Completed { .. })),
            "Flow did not complete"
        );
        Ok(events)
    }
}

pub fn deltas(events: &[RunEvent]) -> Vec<String> {
    events.iter().filter_map(delta_text).collect()
}

pub fn delta_text(event: &RunEvent) -> Option<String> {
    let RunEventKind::Emitted { value, .. } = &event.kind else {
        return None;
    };
    if value.get_key("kind")?.as_str() != Some("delta") {
        return None;
    }
    value.get_key("text")?.as_str().map(str::to_owned)
}

/// A shared graph reference keeps the actual producer's allocation owner.
pub fn loaded_messages(events: &[RunEvent]) -> Option<CoreValue> {
    events.iter().find_map(|event| match &event.kind {
        RunEventKind::StepValue { step, value } if step == "load" => value.get_key("messages"),
        _ => None,
    })
}

pub fn loaded_message_count(events: &[RunEvent]) -> Option<usize> {
    loaded_messages(events)?
        .array()
        .map(|messages| messages.len())
}

pub fn assert_no_plaintext(root: &Path, secret: &[u8]) -> Result<()> {
    for item in std::fs::read_dir(root)? {
        let path = item?.path();
        if path.is_dir() {
            assert_no_plaintext(&path, secret)?;
        } else {
            let data = std::fs::read(path)?;
            anyhow::ensure!(
                !data.windows(secret.len()).any(|window| window == secret),
                "secret plaintext appeared in the data root"
            );
        }
    }
    Ok(())
}
