//! DeepThink is an ordinary Wasm Pack: Flow, typed ports, Gate and private store.

mod input_context;
mod phases;
mod public_progress;
mod repository_adapter;
mod run_repository;
mod runtime_repository;
mod stream_pump;
#[cfg(test)]
mod tests;
mod workflow;

wit_bindgen::generate!({path:"wit",world:"pack",generate_all});

use exports::local::deepthink::{
    input_context as input_context_port,
    workflow::{Guest, Prompt, RecoverySelection},
};
use exports::tobkiri::core1_0_0::{lifecycle, stream_source};
use serde_json::{Value, json};
use std::cell::RefCell;
use std::collections::{BTreeMap, VecDeque};
use std::rc::Rc;
use tobkiri::core1_0_0::{
    flow,
    types::{Error, ErrorCode, StreamHandle},
};
use tobkiri::core1_1_0::{
    storage,
    types::{Error as StorageError, ErrorCode as StorageErrorCode},
};
use workflow::{
    AttemptContext, Failure, Generated, GenerationProgress, Ports, Run, Settings, VERSION, invalid,
};

const LLM: &str = "local.harness-chat.llm-provider@1.0";
const STRUCTURED_LLM: &str = "local.harness-chat.structured-llm-provider@1.0";
const TOOLS: &str = "local.harness-agent.tool-provider@1.0";

struct Active {
    run: Run,
    head: run_repository::Head,
    events: VecDeque<String>,
    announced: bool,
    terminal_sent: bool,
    failure: Option<Failure>,
}

#[derive(Default)]
struct State {
    settings: Option<Settings>,
    next_id: u64,
    streams: BTreeMap<u64, Active>,
}
thread_local! { static STATE: RefCell<State> = RefCell::new(State::default()); }
struct Pack;

fn guest_error(failure: Failure) -> Error {
    Error {
        code: match failure.code.as_str() {
            "cancelled" => ErrorCode::Cancelled,
            "denied" | "blocked" => ErrorCode::Denied,
            "conflict" | "needs-reconciliation" => ErrorCode::Conflict,
            "core-internal" | "checkpoint-unconfirmed" => ErrorCode::Internal,
            "unavailable" => ErrorCode::Unavailable,
            "limit-exceeded" => ErrorCode::LimitExceeded,
            _ => ErrorCode::Invalid,
        },
        message: failure.message,
    }
}

fn failure(error: Error) -> Failure {
    Failure {
        code: match error.code {
            ErrorCode::Cancelled => "cancelled",
            ErrorCode::Denied => "denied",
            ErrorCode::ApprovalRequired => "approval-required",
            ErrorCode::Conflict => "conflict",
            ErrorCode::Unavailable => "unavailable",
            ErrorCode::LimitExceeded => "limit-exceeded",
            ErrorCode::Internal => "core-internal",
            ErrorCode::NotFound => "core-not-found",
            ErrorCode::Invalid => "core-invalid",
        }
        .into(),
        message: error.message,
    }
}

fn storage_error(error: StorageError) -> Error {
    // Storage 1.1 and the public Flow ports are nominally distinct WIT types.
    Error {
        code: match error.code {
            StorageErrorCode::Denied => ErrorCode::Denied,
            StorageErrorCode::ApprovalRequired => ErrorCode::ApprovalRequired,
            StorageErrorCode::Cancelled => ErrorCode::Cancelled,
            StorageErrorCode::NotFound => ErrorCode::NotFound,
            StorageErrorCode::Invalid => ErrorCode::Invalid,
            StorageErrorCode::LimitExceeded => ErrorCode::LimitExceeded,
            StorageErrorCode::Conflict => ErrorCode::Conflict,
            StorageErrorCode::Unavailable => ErrorCode::Unavailable,
            StorageErrorCode::Internal => ErrorCode::Internal,
        },
        message: error.message,
    }
}

fn storage_failure(error: StorageError) -> Failure {
    failure(storage_error(error))
}

fn private_store() -> Result<storage::Store, Failure> {
    storage::open("workflows").map_err(storage_failure)
}

#[derive(Default)]
struct Runtime {
    // Runtime lives only for the current synchronous phase/export. The handle
    // never enters STATE or Active, and every KV call retains its own Gate check.
    store: Option<Rc<storage::Store>>,
    ledger: Option<runtime_repository::RunSession>,
    projection_warning: Option<String>,
}

struct GenerationPump<'runtime, 'progress> {
    runtime: &'runtime mut Runtime,
    public: public_progress::PublicProgress<'progress>,
    stream: StreamHandle,
}

impl stream_pump::Driver for GenerationPump<'_, '_> {
    fn active(&mut self) -> Result<(), Failure> {
        self.runtime.active()
    }

    fn pull(&mut self) -> Result<Vec<String>, Failure> {
        flow::pull(self.stream, 64).map_err(failure)
    }

    fn accept_batch(&mut self, events: Vec<String>) -> Result<(), Failure> {
        let runtime = &mut *self.runtime;
        self.public
            .accept_batch(events, |snapshot| runtime.persist_progress(snapshot))
    }

    fn fail(&mut self, error: Failure) -> Failure {
        let runtime = &mut *self.runtime;
        self.public
            .fail(error, |snapshot| runtime.persist_progress(snapshot))
    }

    fn eof(&mut self) -> Result<(), Failure> {
        let runtime = &mut *self.runtime;
        self.public
            .eof(|snapshot| runtime.persist_progress(snapshot))
    }

    fn cancel(&mut self) {
        flow::cancel_stream(self.stream);
    }
}

impl Runtime {
    fn private_store(&mut self) -> Result<&Rc<storage::Store>, Failure> {
        if self.store.is_none() {
            self.store = Some(Rc::new(private_store()?));
        }
        Ok(self.store.as_ref().expect("store was opened above"))
    }

    fn persist_progress(&mut self, progress: &GenerationProgress) -> Result<(), Failure> {
        let result = self
            .ledger
            .as_mut()
            .ok_or_else(|| invalid("generation requires an authoritative Run ledger"))?
            .save_progress(progress);
        result.map_err(|error: Failure| Failure {
            code: "checkpoint-unconfirmed".into(),
            message: format!(
                "Generation snapshot write was not confirmed; do not automatically repeat the request. {}",
                error.message
            ),
        })
    }

    fn read_progress(
        &mut self,
        attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        if let Some(ledger) = self.ledger.as_mut() {
            return ledger.read_progress(attempt);
        }
        // Only legacy diagnostic reads use preserved KV sidecars. Managed Runs
        // never fall back to KV after a missing/failed SQL progress read.
        self.private_store()?
            .kv_get(&attempt.storage_key())
            .map_err(storage_failure)?
            .map(|bytes| {
                serde_json::from_slice(&bytes)
                    .map_err(|_| invalid("saved generation progress is invalid"))
            })
            .transpose()
    }

    fn managed(head: &run_repository::Head) -> Result<Self, Failure> {
        let store = Rc::new(private_store()?);
        let ledger = runtime_repository::RunSession::resume(Rc::clone(&store), head)?;
        Ok(Self {
            store: Some(store),
            ledger: Some(ledger),
            projection_warning: None,
        })
    }

    fn fresh(run: &Run) -> Result<Self, Failure> {
        let store = Rc::new(private_store()?);
        let ledger = runtime_repository::RunSession::create(Rc::clone(&store), run)?;
        Ok(Self {
            store: Some(store),
            ledger: Some(ledger),
            projection_warning: None,
        })
    }

    fn head(&self) -> Result<run_repository::Head, Failure> {
        self.ledger
            .as_ref()
            .map(|ledger| ledger.head().clone())
            .ok_or_else(|| invalid("workflow has no authoritative Run head"))
    }
}

// Existing eager batch regressions use the production acceptance/rollback path.
#[cfg(test)]
fn record_public_batch(
    progress: &mut GenerationProgress,
    events: Vec<String>,
    persist: impl FnOnce(&GenerationProgress) -> Result<(), Failure>,
) -> Result<(), Failure> {
    public_progress::record_eager_batch(progress, events, persist)
}

fn merge_saved_progress(run: &mut Run) -> Result<(), Failure> {
    let contexts = run
        .attempts
        .values()
        .filter(|attempt| attempt["status"] != "completed")
        .filter(|attempt| {
            attempt
                .pointer("/context/attempt_id")
                .and_then(Value::as_str)
                .is_none_or(|id| !run.generation_resolutions.contains_key(id))
        })
        .filter_map(|attempt| attempt.get("context"))
        .map(|context| {
            serde_json::from_value::<AttemptContext>(context.clone())
                .map_err(|_| invalid("saved attempt binding is invalid"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut runtime = Runtime::default();
    for context in contexts {
        if let Some(progress) = runtime.read_progress(&context)? {
            run.merge_generation_progress(progress)?;
        }
    }
    Ok(())
}

impl Ports for Runtime {
    fn active(&mut self) -> Result<(), Failure> {
        if flow::is_cancelled() {
            Err(Failure {
                code: "cancelled".into(),
                message: "Workflow cancelled; completed checkpoints remain available.".into(),
            })
        } else {
            Ok(())
        }
    }

    fn generate(
        &mut self,
        model: &str,
        messages: &[Value],
        phase: &str,
        attempt: &AttemptContext,
    ) -> Result<Generated, Failure> {
        self.active()?;
        self.ledger
            .as_mut()
            .ok_or_else(|| invalid("generation has no authoritative Run ledger"))?
            .consume_dispatch(attempt)?;
        // Zero is the typed provider's explicit provider-default convention.
        // The provider omits max_tokens instead of imposing an arbitrary cap.
        let structured = STATE.with_borrow(|state| {
            state
                .settings
                .as_ref()
                .is_some_and(|settings| settings.structured_provider)
        });
        let (port, arguments) = generation_port_arguments(model, messages, phase, structured);
        let mut progress = GenerationProgress::new(attempt.clone());
        progress.next_snapshot()?;
        // Sidecar admission must succeed before a paid model call can begin.
        self.persist_progress(&progress)?;
        let stream =
            flow::call_port_stream(port, "chat", &arguments.to_string()).map_err(failure)?;
        let mut pump = GenerationPump {
            runtime: self,
            public: public_progress::PublicProgress::new(&mut progress),
            stream,
        };
        stream_pump::run(&mut pump)?;
        Ok(Generated {
            text: pump.public.snapshot().public_text.clone(),
            usage: pump.public.snapshot().usage.clone(),
        })
    }

    fn generation_progress(
        &mut self,
        attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        self.read_progress(attempt)
    }

    fn claim_generation(&mut self, run: &Run, attempt: &AttemptContext) -> Result<(), Failure> {
        self.active()?;
        self.ledger
            .as_mut()
            .ok_or_else(|| invalid("generation has no authoritative Run ledger"))?
            .claim_generation(run, attempt)
    }

    fn tool(&mut self, call: &Value) -> Result<Value, Failure> {
        self.active()?;
        let raw = flow::call_port(TOOLS, "invoke", &tool_port_arguments(call).to_string())
            .map_err(failure)?;
        // A json WIT scalar is encoded as a JSON string by the generic port API.
        let decoded: Value =
            serde_json::from_str(&raw).map_err(|_| invalid("tool port returned invalid JSON"))?;
        match decoded {
            Value::String(value) => {
                serde_json::from_str(&value).map_err(|_| invalid("tool result JSON is malformed"))
            }
            value => Ok(value),
        }
    }

    fn verify_artifact(&mut self, path: &str) -> Result<Value, Failure> {
        let result = self.tool(&json!({"name":"artifact.stat","arguments":{"path":path}}))?;
        if result["ok"] != true
            || result["result"]["exists"] != true
            || result["result"]["path"] != path
            || result["result"]["digest"]
                .as_str()
                .is_none_or(|digest| !digest.starts_with("sha256:"))
        {
            return Err(invalid(
                "artifact owner could not verify the claimed stored artifact",
            ));
        }
        Ok(
            json!({"kind":"tool-owned-storage-artifact","port":TOOLS,"path":path,"proof":result["result"]}),
        )
    }

    fn persist(&mut self, run: &Run) -> Result<(), Failure> {
        self.ledger
            .as_mut()
            .ok_or_else(|| invalid("legacy workflow requires verified migration before writes"))?
            .persist(run)?;
        // Readable KV projections do not replace the confirmed SQL authority.
        // A failed projection is surfaced separately and cannot uncommit a Run.
        if let Err(error) = self.project(run) {
            self.projection_warning = Some(error.message);
        }
        Ok(())
    }
}

impl Runtime {
    fn project(&mut self, run: &Run) -> Result<(), Failure> {
        let store = self.private_store()?;
        let mut projections = vec![
            ("goal.json", run.goal.to_string()),
            ("goal.md", run.goal_markdown()),
        ];
        if !run.candidate.is_empty() {
            let name = if run.status == "completed" {
                "answer.md"
            } else if run.status == "quarantined" {
                "quarantine/candidate.md"
            } else {
                "draft.md"
            };
            projections.push((name, run.candidate.clone()));
        }
        for (name, content) in projections {
            if content.len() > 1024 * 1024 {
                return Err(invalid(
                    "Readable KV projection exceeds its bound; authoritative SQL Run remains retained",
                ));
            }
            store.kv_put(&format!("{}/{name}",run.id),content.as_bytes())
                .map_err(|_|invalid("Readable KV projection was not confirmed; authoritative SQL Run remains retained"))?;
        }
        Ok(())
    }
}

impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let settings: Settings = serde_json::from_str(&config)
            .map_err(|_| guest_error(invalid("DeepThink configuration is invalid")))?;
        settings.validate().map_err(guest_error)?;
        STATE.with_borrow_mut(|state| {
            state.settings = Some(settings);
            state.streams.clear();
        });
        private_store().map_err(guest_error)?;
        Ok(())
    }
    fn shutdown() {
        STATE.with_borrow_mut(|state| {
            state.streams.clear();
            state.settings = None;
        });
    }
}

fn head_value(head: &run_repository::Head) -> Value {
    json!({"format":"deepthink.run-ledger.v1","run_id":head.run_id,
        "revision":head.revision.to_string(),"snapshot_digest":head.snapshot_digest.as_str(),
        "run_request_digest":head.run_request_digest.as_str()})
}

fn selected_run(selection: &RecoverySelection) -> Result<(Run, run_repository::Head), Error> {
    let (run, head) = managed_run(load_run(&selection.run_id)?)?;
    let revision = selection
        .expected_revision
        .parse::<i64>()
        .map_err(|_| guest_error(invalid("invalid expected Run revision")))?;
    let expected_digest = run_repository::Digest::parse(&selection.expected_snapshot_digest)
        .map_err(runtime_repository::repository_failure)
        .map_err(guest_error)?;
    if revision < 0
        || revision.to_string() != selection.expected_revision
        || revision != head.revision
        || expected_digest != head.snapshot_digest
        || selection.conversation != run.conversation
    {
        return Err(guest_error(Failure {
            code: "conflict".into(),
            message: "Recovery selection changed its saved Run head or conversation.".into(),
        }));
    }
    let settings = STATE
        .with_borrow(|state| state.settings.clone())
        .ok_or_else(|| guest_error(invalid("DeepThink has not been initialized")))?;
    if workflow::digest(&json!(settings)) != workflow::digest(&json!(run.settings)) {
        return Err(guest_error(invalid(
            "Recovery changed its exact captured configuration",
        )));
    }
    Ok((run, head))
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestedObservation {
    version: String,
    requested_model: String,
    model_mapping: workflow::reconciliation::ModelMapping,
    observation: workflow::reconciliation::ReaderObservation,
}

fn reconcile_generation(selection: RecoverySelection) -> Result<String, Error> {
    use workflow::reconciliation as recovery;
    let (mut run, head) = selected_run(&selection)?;
    if let Some(resolution) = run.generation_resolutions.get(&selection.attempt_id) {
        run.validate_resolution(&selection.attempt_id, resolution)
            .map_err(guest_error)?;
        return Ok(json!({"run_id":run.id,"repository":head_value(&head),
            "resolution":resolution,"usage":run.usage(),"existing_immutable_receipt":true})
        .to_string());
    }
    let attempt = run
        .attempts
        .get(&selection.attempt_id)
        .ok_or_else(|| guest_error(invalid("original model attempt was not found")))?;
    let generation = attempt
        .pointer("/progress/metadata/generation_id")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            guest_error(invalid(
                "the pending attempt has no retained provider generation id",
            ))
        })?;
    let provider = attempt
        .pointer("/progress/metadata/provider")
        .and_then(Value::as_str);
    let arguments = json!({"request":{"generation-id":generation,"expected-model":run.model,
        "expected-provider":provider,"include-public-content":true}});
    // The User supplies only an attempt/head selection, never terminal/cost JSON.
    // The configured ordinary reader verifies its catalog and fixed GETs through
    // its own current Gate, Network and opaque Secret authority.
    let raw = flow::call_port(
        recovery::READER_PORT,
        "lookup-requested",
        &arguments.to_string(),
    )?;
    let outer: Value = serde_json::from_str(&raw)
        .map_err(|_| guest_error(invalid("reader result is not valid JSON")))?;
    let value = match outer {
        Value::String(text) => serde_json::from_str(&text)
            .map_err(|_| guest_error(invalid("reader JSON result is invalid")))?,
        value => value,
    };
    let observed: RequestedObservation = serde_json::from_value(value).map_err(|_| {
        guest_error(invalid(
            "requested generation observation has an invalid closed schema",
        ))
    })?;
    if observed.version != "tobkiri.requested-generation-observation.v1"
        || observed.requested_model != run.model
    {
        return Err(guest_error(invalid(
            "reader changed the original requested model",
        )));
    }
    let canonical_model = observed
        .observation
        .metadata
        .model
        .clone()
        .ok_or_else(|| guest_error(invalid("reader did not confirm the observed model")))?;
    let provenance = recovery::ObservationProvenance {
        version: recovery::PROVENANCE_VERSION.into(),
        reader_pack: "local.openrouter-generation-reader".into(),
        reader_contract: recovery::READER_PORT.into(),
        reader_pack_hash: None,
        reader_binding_digest: None,
        profile_revision: None,
        requested_model: run.model.clone(),
        canonical_model,
        observation_digest: workflow::digest(&json!(observed.observation)),
        model_mapping: Some(observed.model_mapping),
    };
    let resolution = run
        .reconcile_generation(&selection.attempt_id, observed.observation, provenance)
        .map_err(guest_error)?;
    // A changed head or progress stamp during those GETs is rejected by the
    // repository; a lookup never holds a SQL transaction or dispatch permit.
    let mut runtime = Runtime::managed(&head).map_err(guest_error)?;
    runtime.persist(&run).map_err(guest_error)?;
    let updated = runtime.head().map_err(guest_error)?;
    Ok(
        json!({"run_id":run.id,"repository":head_value(&updated),"resolution":resolution,
        "usage":run.usage(),"existing_immutable_receipt":false,
        "projection_warning":runtime.projection_warning})
        .to_string(),
    )
}

struct LoadedRun {
    run: Run,
    head: Option<run_repository::Head>,
}

fn load_optional_run(run_id: &str) -> Result<Option<LoadedRun>, Error> {
    if run_id.len() != 64 || !run_id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(guest_error(invalid("invalid run reference")));
    }
    let store = Rc::new(private_store().map_err(guest_error)?);
    if let Some((run, head)) =
        runtime_repository::RunSession::load(Rc::clone(&store), run_id).map_err(guest_error)?
    {
        return Ok(Some(LoadedRun {
            run,
            head: Some(head),
        }));
    }
    let Some(bytes) = store
        .kv_get(&format!("{run_id}/checkpoint"))
        .map_err(storage_error)?
    else {
        return Ok(None);
    };
    let mut run = serde_json::from_slice(&bytes)
        .map_err(|_| guest_error(invalid("checkpoint is invalid")))?;
    merge_saved_progress(&mut run).map_err(guest_error)?;
    Ok(Some(LoadedRun { run, head: None }))
}

fn load_run(run_id: &str) -> Result<LoadedRun, Error> {
    load_optional_run(run_id)?.ok_or_else(|| guest_error(invalid("workflow was not found")))
}

fn managed_run(loaded: LoadedRun) -> Result<(Run, run_repository::Head), Error> {
    let head = loaded.head.ok_or_else(||guest_error(Failure {
        code:"needs-reconciliation".into(),
        message:"This legacy checkpoint is preserved. Migration requires verified retirement of its old execution; a User flag or a Profile epoch change cannot supply that proof.".into(),
    }))?;
    Ok((loaded.run, head))
}

fn register(mut run: Run, previous: Option<run_repository::Head>) -> Result<StreamHandle, Error> {
    // Refuse before writing a continuation checkpoint. An existing stream may
    // still own the paused snapshot until its final empty pull closes it.
    STATE.with_borrow(|state| {
        if state.streams.len() >= 64 || state.next_id == u64::MAX {
            return Err(guest_error(invalid(
                "workflow stream capacity is exhausted",
            )));
        }
        if state.streams.values().any(|active| active.run.id == run.id) {
            return Err(guest_error(Failure {
                code: "conflict".into(),
                message: "This workflow is already active.".into(),
            }));
        }
        Ok(())
    })?;
    if run.status == "running" {
        run.error = None;
    }
    let mut runtime = if let Some(previous) = previous {
        let mut runtime = Runtime::managed(&previous).map_err(guest_error)?;
        runtime.persist(&run).map_err(guest_error)?;
        runtime
    } else {
        let mut runtime = Runtime::fresh(&run).map_err(guest_error)?;
        if let Err(error) = runtime.project(&run) {
            runtime.projection_warning = Some(error.message);
        }
        runtime
    };
    let head = runtime.head().map_err(guest_error)?;
    let mut initial_events = VecDeque::new();
    if let Some(message) = runtime.projection_warning.take() {
        initial_events.push_back(event(
            "progress",
            Some(json!({"kind":"storage-projection-warning","message":message}).to_string()),
        ));
    }
    STATE.with_borrow_mut(|state| {
        if state.streams.len() >= 64 {
            return Err(guest_error(invalid("too many active workflow streams")));
        }
        if state.streams.values().any(|active| active.run.id == run.id) {
            return Err(guest_error(Failure {
                code: "conflict".into(),
                message: "This workflow is already active.".into(),
            }));
        }
        state.next_id = state
            .next_id
            .checked_add(1)
            .ok_or_else(|| guest_error(invalid("stream handle exhaustion")))?;
        let id = state.next_id;
        if run.status == "running" {
            run.error = None;
        }
        state.streams.insert(
            id,
            Active {
                run,
                head,
                events: initial_events,
                announced: false,
                terminal_sent: false,
                failure: None,
            },
        );
        Ok(StreamHandle { id })
    })
}

impl Guest for Pack {
    fn run(
        prompt: Prompt,
        conversation: String,
        resume: Option<String>,
    ) -> Result<StreamHandle, Error> {
        let settings = STATE
            .with_borrow(|state| state.settings.clone())
            .ok_or_else(|| guest_error(invalid("DeepThink has not been initialized")))?;
        let messages = prompt
            .messages
            .into_iter()
            .map(|message| json!({"role":message.role,"content":message.content}))
            .collect();
        let expected =
            Run::new(conversation, prompt.model, messages, settings).map_err(guest_error)?;
        let explicit_resume = resume.is_some();
        let identifier = resume.unwrap_or_else(|| expected.id.clone());
        if identifier.len() != 64 || !identifier.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(guest_error(invalid("invalid run reference")));
        }
        let store = private_store().map_err(guest_error)?;
        if !explicit_resume {
            let previous = expected.previous_v4_storage_id();
            if let Some(bytes) = store
                .kv_get(&format!("{previous}/checkpoint"))
                .map_err(storage_error)?
            {
                let previous: Run = serde_json::from_slice(&bytes)
                    .map_err(|_| guest_error(invalid("prior v4 checkpoint is invalid")))?;
                return Err(guest_error(expected.previous_v4_conflict(&previous)));
            }
        }
        let stored = load_optional_run(&identifier)?;
        let (run, head) = if let Some(loaded) = stored {
            let (mut run, head) = managed_run(loaded)?;
            if explicit_resume {
                run.resume_pinned(&expected.conversation, &expected.model, &expected.settings)
                    .map_err(guest_error)?;
            } else {
                if run.status == "paused" {
                    return Err(guest_error(Failure {
                        code: "conflict".into(),
                        message: "This request has a saved phase boundary. Use its run reference through continuation-input to continue; it was not automatically restarted.".into(),
                    }));
                }
                run.resume(&expected).map_err(guest_error)?;
            }
            (run, Some(head))
        } else {
            if identifier != expected.id {
                return Err(guest_error(invalid("resume run was not found")));
            }
            (expected, None)
        };
        register(run, head)
    }

    fn continue_run(
        run_id: String,
        conversation: String,
        answers: Option<String>,
    ) -> Result<StreamHandle, Error> {
        let (mut run, head) = managed_run(load_run(&run_id)?)?;
        let settings = STATE
            .with_borrow(|state| state.settings.clone())
            .ok_or_else(|| guest_error(invalid("DeepThink has not been initialized")))?;
        if let Some(answer) = answers {
            run.answer(&conversation, &answer, &settings)
                .map_err(guest_error)?;
        } else {
            run.resume_pinned(&conversation, &run.model.clone(), &settings)
                .map_err(guest_error)?;
        }
        register(run, Some(head))
    }

    fn status(run_id: String) -> Result<String, Error> {
        if run_id.len() != 64 || !run_id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(guest_error(invalid("invalid run reference")));
        }
        let loaded = load_run(&run_id)?;
        let repository = loaded.head.as_ref().map(head_value);
        let run = loaded.run;
        let reconciliation = run.ensure_generations_reconciled().err();
        // The candidate is only exposed on this explicitly authorized diagnostic
        // port; the assistant stream never emits a rejected candidate as output.
        Ok(
            json!({"version":run.version,"run_id":run.id,"status":if reconciliation.is_some(){"needs_reconciliation"}else{&run.status},"goal":run.goal,
            "phases":run.phases,"usage":run.usage(),"artifacts":run.artifacts,
            "capability_gaps":run.settings.capability_gaps,"error":run.error,"output":run.output(),
            "reconciliation":reconciliation,"model_attempts":run.attempts,
            "repository":repository,"generation_resolutions":run.generation_resolutions,
            "regeneration_authorizations":run.regeneration_authorizations})
            .to_string(),
        )
    }

    fn reconcile_generation(selection: RecoverySelection) -> Result<String, Error> {
        reconcile_generation(selection)
    }

    fn regenerate_phase(
        selection: RecoverySelection,
        resolution_digest: String,
    ) -> Result<StreamHandle, Error> {
        let (mut run, head) = selected_run(&selection)?;
        run.prepare_regeneration(&selection.attempt_id, &resolution_digest)
            .map_err(guest_error)?;
        register(run, Some(head))
    }
}

impl input_context_port::Guest for Pack {
    fn continuation(
        run_id: String,
        conversation: String,
        answers: Option<String>,
    ) -> Result<input_context_port::Context, Error> {
        // These reads are reached only as a normal worker step after the
        // separate input adapter has entered the selected continuation Flow.
        let (run, _) = managed_run(load_run(&run_id)?)?;
        let context = input_context::continuation(&run, &conversation, answers.as_deref())
            .map_err(guest_error)?;
        Ok(input_context_port::Context {
            conversation: context.conversation,
            history_text: context.history_text,
        })
    }

    fn regeneration(
        selection: input_context_port::RecoverySelection,
    ) -> Result<input_context_port::Context, Error> {
        // Keep the existing saved-head/configuration/conversation admission.
        // This preparation does not resolve or authorize a model attempt.
        let (run, _) = selected_run(&selection)?;
        let context = input_context::regeneration(&run).map_err(guest_error)?;
        Ok(input_context_port::Context {
            conversation: context.conversation,
            history_text: context.history_text,
        })
    }

    fn control_result(op: String, value: String) -> Result<String, Error> {
        input_context::control_result(&op, &value).map_err(guest_error)
    }
}

fn tool_port_arguments(call: &Value) -> Value {
    // Core's typed Json codec serializes this object for the guest exactly
    // once. Supplying a JSON string here would double-encode the tool call.
    json!({"call": call})
}

fn generation_port_arguments(
    model: &str,
    messages: &[Value],
    phase: &str,
    structured: bool,
) -> (&'static str, Value) {
    let mut arguments = json!({"prompt":{"model":model,"messages":messages,"max-tokens":0}});
    if structured {
        let name = format!("deepthink_{}", &workflow::digest(&json!(phase))[..24]);
        // Json must remain an object here. Core's typed codec serializes it
        // once for the receiving guest, just like the real-tool call port.
        arguments["response-format"] =
            json!({"name":name,"schema":phases::schema(phase),"strict":true});
        (STRUCTURED_LLM, arguments)
    } else {
        (LLM, arguments)
    }
}

fn event(kind: &str, text: Option<String>) -> String {
    json!({"kind":kind,"text":text,"prompt-tokens":null,"completion-tokens":null}).to_string()
}

impl Active {
    fn next(&mut self, max: usize) -> Result<Vec<String>, Failure> {
        if !self.events.is_empty() {
            return Ok((0..max).filter_map(|_| self.events.pop_front()).collect());
        }
        if let Some(error) = self.failure.take() {
            return Err(error);
        }
        if self.terminal_sent {
            return Ok(vec![]);
        }
        if self.run.status == "running" {
            if !self.announced {
                self.announced = true;
                return Ok(vec![event("progress",Some(json!({"version":VERSION,"run_id":self.run.id,"phase":self.run.pending.front().map(|phase|phase.id.as_str()),"status":"running"}).to_string()))]);
            }
            let mut runtime = Runtime::managed(&self.head)?;
            let outcome = self.run.advance_segment(&mut runtime);
            self.head = runtime.head()?;
            if let Some(message) = runtime.projection_warning.take() {
                self.events.push_back(event(
                    "progress",
                    Some(
                        json!({"kind":"storage-projection-warning","message":message}).to_string(),
                    ),
                ));
            }
            match outcome {
                Ok(events) => {
                    self.announced = false;
                    for value in events {
                        self.events
                            .push_back(event("progress", Some(value.to_string())));
                    }
                }
                Err(error) => {
                    self.failure = Some(error.clone());
                    self.events.push_back(event("progress",Some(json!({"version":VERSION,"run_id":self.run.id,"status":"quarantined","error":error,"output":""}).to_string())));
                }
            }
        } else if self.run.status == "paused" {
            let receipt = self.run.pause_receipt()?;
            self.terminal_sent = true;
            let usage = self.run.usage();
            self.events.push_back(json!({"kind":"usage","text":usage.to_string(),
                "prompt-tokens":usage.pointer("/tokens/input_tokens"),"completion-tokens":usage.pointer("/tokens/output_tokens")}).to_string());
            self.events
                .push_back(event("progress", Some(receipt.to_string())));
            self.events
                .push_back(event("paused", Some(receipt.to_string())));
        } else if self.run.status == "quarantined" {
            return Err(self
                .run
                .error
                .clone()
                .unwrap_or_else(|| invalid("workflow is quarantined; checkpoints are retained")));
        } else {
            self.terminal_sent = true;
            let usage = self.run.usage();
            self.events.push_back(json!({"kind":"usage","text":usage.to_string(),
                "prompt-tokens":usage.pointer("/tokens/input_tokens"),"completion-tokens":usage.pointer("/tokens/output_tokens")}).to_string());
            self.events.push_back(event("progress",Some(json!({"version":VERSION,"run_id":self.run.id,"status":self.run.status,"approved":self.run.status=="completed","artifacts":self.run.artifacts,"capability_gaps":self.run.settings.capability_gaps}).to_string())));
            self.events
                .push_back(event("delta", Some(self.run.output())));
            self.events
                .push_back(event("done", Some(self.run.output())));
        }
        Ok((0..max).filter_map(|_| self.events.pop_front()).collect())
    }
}

impl stream_source::Guest for Pack {
    fn pull(local_id: u64, max: u32) -> Result<Vec<String>, Error> {
        if max == 0 || max > 64 {
            return Err(guest_error(invalid("pull batch must be 1..64")));
        }
        let mut active = STATE
            .with_borrow_mut(|state| state.streams.remove(&local_id))
            .ok_or_else(|| guest_error(invalid("workflow stream was not found")))?;
        let result = active.next(max as usize);
        if result.as_ref().is_ok_and(|events| !events.is_empty()) {
            STATE.with_borrow_mut(|state| {
                state.streams.insert(local_id, active);
            });
        }
        result.map_err(guest_error)
    }
    fn cancel(local_id: u64) {
        // Each completed transition and every pending effect were persisted
        // before suspension. Cancellation cannot acquire new storage authority.
        STATE.with_borrow_mut(|state| {
            state.streams.remove(&local_id);
        });
    }
}

#[cfg(test)]
mod runtime_progress_tests {
    use super::*;

    fn progress() -> GenerationProgress {
        GenerationProgress::new(AttemptContext {
            run_id: "0".repeat(64),
            run_request_digest: "1".repeat(64),
            phase: "goal_intake".into(),
            generation_key: "goal_intake@0:0#0".into(),
            attempt_id: "goal_intake@0:0#0:0".into(),
            request_digest: "2".repeat(64),
            route_digest: "3".repeat(64),
        })
    }

    fn rejected(
        mut progress: GenerationProgress,
        events: Vec<String>,
    ) -> (GenerationProgress, GenerationProgress, Failure) {
        let mut saved = None;
        let error = record_public_batch(&mut progress, events, |snapshot| {
            saved = Some(snapshot.clone());
            Ok(())
        })
        .unwrap_err();
        let saved = saved.expect("the accepted public prefix must be saved first");
        assert_eq!(progress, saved);
        assert!(!saved.completed);
        assert_eq!(saved.sequence, 1);
        assert_eq!(saved.public_text, "391");
        (progress, saved, error)
    }

    #[test]
    fn accepted_prefix_is_saved_before_regressing_usage_without_partial_fields() {
        let mut progress = progress();
        progress.usage = json!({"input_tokens":5,"output_tokens":9});
        let (_, saved, error) = rejected(
            progress,
            vec![
                json!({"kind":"delta","text":"391"}).to_string(),
                json!({"kind":"usage","prompt-tokens":6,"completion-tokens":8}).to_string(),
            ],
        );
        assert_eq!(saved.usage, json!({"input_tokens":5,"output_tokens":9}));
        assert_eq!(error.message, "provider usage counter decreased");
    }

    #[test]
    fn accepted_prefix_is_saved_before_identity_failure_without_partial_fields() {
        for (previous, invalid_metadata) in [
            (
                json!({"generation_id":"gen-original"}),
                json!({"generation_id":"gen-changed"}),
            ),
            (
                json!({"model":"original"}),
                json!({"generation_id":"gen-new","model":"changed"}),
            ),
        ] {
            let mut progress = progress();
            progress.metadata = previous.clone();
            let mut metadata = invalid_metadata;
            metadata["version"] = json!("tobkiri.chat.metadata.v1");
            let (_, saved, error) = rejected(
                progress,
                vec![
                    json!({"kind":"delta","text":"391"}).to_string(),
                    json!({"kind":"metadata","text":metadata.to_string()}).to_string(),
                ],
            );
            assert_eq!(saved.metadata, previous);
            assert_eq!(
                error.message,
                "provider changed or invalidated its public identity"
            );
        }
    }

    #[test]
    fn accepted_prefix_is_saved_before_mismatching_done_without_completion() {
        let mut progress = progress();
        progress.public_text = "39".into();
        let (_, _, error) = rejected(
            progress,
            vec![
                json!({"kind":"delta","text":"1"}).to_string(),
                json!({"kind":"done","text":"392"}).to_string(),
            ],
        );
        assert_eq!(
            error.message,
            "provider completion differs from its public deltas"
        );
    }

    #[test]
    fn accepted_prefix_is_saved_before_a_malformed_later_event() {
        let (_, _, error) = rejected(
            progress(),
            vec![
                json!({"kind":"delta","text":"391"}).to_string(),
                "invalid-event-json".into(),
            ],
        );
        assert_eq!(error.message, "provider returned an invalid stream event");
    }

    #[test]
    fn prefix_write_failure_retains_checkpoint_unconfirmed_instead_of_event_error() {
        let mut progress = progress();
        let mut writes = 0;
        let error = record_public_batch(
            &mut progress,
            vec![
                json!({"kind":"delta","text":"391"}).to_string(),
                json!({"kind":"done","text":"wrong"}).to_string(),
            ],
            |snapshot| {
                writes += 1;
                assert_eq!(snapshot.public_text, "391");
                assert!(!snapshot.completed);
                Err(Failure {
                    code: "checkpoint-unconfirmed".into(),
                    message: "write not confirmed".into(),
                })
            },
        )
        .unwrap_err();
        assert_eq!(writes, 1);
        assert_eq!(error.code, "checkpoint-unconfirmed");
        assert_eq!(error.message, "write not confirmed");
    }

    #[test]
    fn private_only_heartbeat_never_writes_a_progress_snapshot() {
        let mut progress = progress();
        let before = progress.clone();
        record_public_batch(
            &mut progress,
            vec![json!({"kind":"progress","text":"untrusted-private-marker"}).to_string()],
            |_| panic!("a content-free heartbeat must not cause a write"),
        )
        .unwrap();
        assert_eq!(progress, before);
    }
}

export!(Pack);
