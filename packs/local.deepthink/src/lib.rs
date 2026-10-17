//! DeepThink is an ordinary Wasm Pack: Flow, typed ports, Gate and private store.

mod phases;
#[cfg(test)]
mod tests;
mod workflow;

wit_bindgen::generate!({path:"wit",world:"pack",generate_all});

use exports::local::deepthink::{
    continuation_input,
    workflow::{Guest, Prompt},
};
use exports::tobkiri::core::{lifecycle, stream_source};
use serde_json::{Value, json};
use std::cell::RefCell;
use std::collections::{BTreeMap, VecDeque};
use tobkiri::core::{
    flow, storage,
    types::{Error, ErrorCode, StreamHandle},
};
use workflow::{
    AttemptContext, Failure, Generated, GenerationProgress, Ports, Run, Settings, VERSION, invalid,
};

const LLM: &str = "local.harness-chat.llm-provider@1.0";
const STRUCTURED_LLM: &str = "local.harness-chat.structured-llm-provider@1.0";
const TOOLS: &str = "local.harness-agent.tool-provider@1.0";

struct Active {
    run: Run,
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

fn private_store() -> Result<storage::Store, Failure> {
    storage::open("workflows").map_err(failure)
}

fn persist_progress(progress: &GenerationProgress) -> Result<(), Failure> {
    let bytes = serde_json::to_vec(progress)
        .map_err(|_| invalid("generation progress cannot be serialized"))?;
    // The attempt intent was already committed before this request. A failed or
    // unknown write never permits another POST; the older intent stays blocking.
    let result = (|| {
        private_store()?
            .kv_put(&progress.attempt.storage_key(), &bytes)
            .map_err(failure)
    })();
    result.map_err(|error: Failure| Failure {
        code:"checkpoint-unconfirmed".into(), message:format!("Generation snapshot write was not confirmed; do not automatically repeat the request. {}", error.message)
    })
}

fn read_progress(attempt: &AttemptContext) -> Result<Option<GenerationProgress>, Failure> {
    private_store()?
        .kv_get(&attempt.storage_key())
        .map_err(failure)?
        .map(|bytes| {
            serde_json::from_slice(&bytes)
                .map_err(|_| invalid("saved generation progress is invalid"))
        })
        .transpose()
}

fn merge_saved_progress(run: &mut Run) -> Result<(), Failure> {
    let contexts = run
        .attempts
        .values()
        .filter(|attempt| attempt["status"] != "completed")
        .filter_map(|attempt| attempt.get("context"))
        .map(|context| {
            serde_json::from_value::<AttemptContext>(context.clone())
                .map_err(|_| invalid("saved attempt binding is invalid"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    for context in contexts {
        if let Some(progress) = read_progress(&context)? {
            run.merge_generation_progress(progress)?;
        }
    }
    Ok(())
}

struct Runtime;
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
        // Sidecar admission must succeed before a paid model call can begin.
        persist_progress(&progress)?;
        let stream =
            flow::call_port_stream(port, "chat", &arguments.to_string()).map_err(failure)?;
        let outcome = (|| {
            loop {
                self.active()?;
                let events = flow::pull(stream, 64).map_err(failure)?;
                if events.is_empty() {
                    break;
                }
                let before = (
                    progress.public_text.len(),
                    progress.completed,
                    progress.usage.clone(),
                    progress.metadata.clone(),
                );
                for event in events {
                    let event: Value = serde_json::from_str(&event)
                        .map_err(|_| invalid("provider returned an invalid stream event"))?;
                    progress.accept(&event)?;
                }
                if before
                    != (
                        progress.public_text.len(),
                        progress.completed,
                        progress.usage.clone(),
                        progress.metadata.clone(),
                    )
                {
                    progress.next_snapshot()?;
                    // Persist changed public evidence before the next blocking
                    // pull. Private-only heartbeats cause no checkpoint write.
                    // A terminal snapshot also precedes Generated delivery.
                    persist_progress(&progress)?;
                }
            }
            if !progress.completed {
                return Err(invalid(
                    "provider stream ended before successful completion",
                ));
            }
            Ok(Generated {
                text: progress.public_text.clone(),
                usage: progress.usage.clone(),
            })
        })();
        if outcome.is_err() {
            flow::cancel_stream(stream);
        }
        outcome
    }

    fn generation_progress(
        &mut self,
        attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        read_progress(attempt)
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
        let store = private_store()?;
        let checkpoint = serde_json::to_vec(run)
            .map_err(|_| invalid("workflow checkpoint cannot be serialized"))?;
        // This single atomic KV value is authoritative on resume. The following
        // readable artifacts are projections, never partially restored state.
        store
            .kv_put(&format!("{}/checkpoint", run.id), &checkpoint)
            .map_err(failure)?;
        store
            .kv_put(
                &format!("{}/goal.json", run.id),
                run.goal.to_string().as_bytes(),
            )
            .map_err(failure)?;
        store
            .kv_put(
                &format!("{}/goal.md", run.id),
                run.goal_markdown().as_bytes(),
            )
            .map_err(failure)?;
        if !run.candidate.is_empty() {
            let name = if run.status == "completed" {
                "answer.md"
            } else if run.status == "quarantined" {
                "quarantine/candidate.md"
            } else {
                "draft.md"
            };
            store
                .kv_put(&format!("{}/{name}", run.id), run.candidate.as_bytes())
                .map_err(failure)?;
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

fn load_run(run_id: &str) -> Result<Run, Error> {
    if run_id.len() != 64 || !run_id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(guest_error(invalid("invalid run reference")));
    }
    let bytes = private_store()
        .map_err(guest_error)?
        .kv_get(&format!("{run_id}/checkpoint"))?
        .ok_or_else(|| guest_error(invalid("workflow was not found")))?;
    let mut run = serde_json::from_slice(&bytes)
        .map_err(|_| guest_error(invalid("checkpoint is invalid")))?;
    merge_saved_progress(&mut run).map_err(guest_error)?;
    Ok(run)
}

fn register(mut run: Run) -> Result<StreamHandle, Error> {
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
    Runtime.persist(&run).map_err(guest_error)?;
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
                events: VecDeque::new(),
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
        let stored = private_store()
            .map_err(guest_error)?
            .kv_get(&format!("{identifier}/checkpoint"))?;
        let run = if let Some(bytes) = stored {
            let mut run: Run = serde_json::from_slice(&bytes)
                .map_err(|_| guest_error(invalid("stored checkpoint is invalid")))?;
            merge_saved_progress(&mut run).map_err(guest_error)?;
            if explicit_resume {
                run.resume_pinned(&expected.conversation, &expected.model, &expected.settings)
                    .map_err(guest_error)?;
            } else {
                run.resume(&expected).map_err(guest_error)?;
            }
            run
        } else {
            if identifier != expected.id {
                return Err(guest_error(invalid("resume run was not found")));
            }
            expected
        };
        register(run)
    }

    fn continue_run(
        run_id: String,
        conversation: String,
        answers: Option<String>,
    ) -> Result<StreamHandle, Error> {
        let mut run = load_run(&run_id)?;
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
        register(run)
    }

    fn status(run_id: String) -> Result<String, Error> {
        if run_id.len() != 64 || !run_id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(guest_error(invalid("invalid run reference")));
        }
        let run = load_run(&run_id)?;
        let reconciliation = run.ensure_generations_reconciled().err();
        // The candidate is only exposed on this explicitly authorized diagnostic
        // port; the assistant stream never emits a rejected candidate as output.
        Ok(
            json!({"version":run.version,"run_id":run.id,"status":if reconciliation.is_some(){"needs_reconciliation"}else{&run.status},"goal":run.goal,
            "phases":run.phases,"usage":run.usage(),"artifacts":run.artifacts,
            "capability_gaps":run.settings.capability_gaps,"error":run.error,"output":run.output(),
            "reconciliation":reconciliation,"model_attempts":run.attempts})
            .to_string(),
        )
    }
}

impl continuation_input::Guest for Pack {
    fn submit(
        run_id: String,
        conversation: String,
        answers: Option<String>,
    ) -> Result<StreamHandle, Error> {
        let run = load_run(&run_id)?;
        if run.conversation != conversation {
            return Err(guest_error(invalid("continuation conversation changed")));
        }
        let history = answers.clone().unwrap_or_else(|| {
            let request = run
                .messages
                .iter()
                .rev()
                .find(|message| message["role"] == "user")
                .and_then(|message| message["content"].as_str())
                .unwrap_or("元のGoal");
            format!("[DeepThink 再開 {run_id}] {request}")
        });
        flow::run_flow("local.deepthink.continuation-flow@1.0",&json!({
            "run-id":run_id,"conversation":conversation,"answers":answers,"history-text":history,
        }).to_string())
    }

    fn pull(stream: StreamHandle, max: u32) -> Result<Vec<continuation_input::ChatEvent>, Error> {
        if max == 0 || max > 64 {
            return Err(guest_error(invalid("pull batch must be 1..64")));
        }
        let frames = flow::pull(stream, max)?;
        let mut events = vec![];
        for frame in frames {
            let frame: Value = serde_json::from_str(&frame)
                .map_err(|_| guest_error(invalid("continuation Flow returned malformed frame")))?;
            let kind = frame
                .pointer("/kind/kind")
                .and_then(Value::as_str)
                .ok_or_else(|| guest_error(invalid("continuation Flow has no event kind")))?;
            let value = match kind {
                "emitted" => frame
                    .pointer("/kind/value")
                    .cloned()
                    .ok_or_else(|| guest_error(invalid("continuation emission is missing")))?,
                "completed" => json!({"kind":"completed"}),
                "failed" => {
                    return Err(guest_error(invalid(
                        "continuation Flow failed; its checkpoints remain available",
                    )));
                }
                "cancelled" => {
                    return Err(guest_error(Failure {
                        code: "cancelled".into(),
                        message: "Continuation cancelled; checkpoints remain available.".into(),
                    }));
                }
                _ => json!({"kind":"progress"}),
            };
            events.push(continuation_input::ChatEvent {
                kind: value["kind"].as_str().unwrap_or("progress").into(),
                text: value["text"].as_str().map(String::from),
                prompt_tokens: value["prompt-tokens"]
                    .as_u64()
                    .and_then(|tokens| u32::try_from(tokens).ok()),
                completion_tokens: value["completion-tokens"]
                    .as_u64()
                    .and_then(|tokens| u32::try_from(tokens).ok()),
            });
        }
        Ok(events)
    }
    fn cancel(stream: StreamHandle) {
        flow::cancel_stream(stream);
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
            match self.run.advance(&mut Runtime) {
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

export!(Pack);
