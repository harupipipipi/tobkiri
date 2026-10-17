//! Resumable domain workflow. Every effect is supplied by an explicit port.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

use crate::phases;

pub const VERSION: &str = "deepthink.workflow.v5";
pub const PROGRESS_VERSION: &str = "deepthink.generation-progress.v1";

fn default_contract_retries() -> u32 {
    2
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tool {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Skill {
    pub name: String,
    pub description: String,
    pub content: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileCheck {
    pub id: String,
    pub label: String,
    pub instructions: String,
    pub required: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub max_review_rounds: u32,
    pub max_tool_rounds: u32,
    #[serde(default = "default_contract_retries")]
    pub max_contract_retries: u32,
    #[serde(default)]
    pub structured_provider: bool,
    pub tools: Vec<Tool>,
    pub skills: Vec<Skill>,
    pub profile_checks: Vec<ProfileCheck>,
    pub environment: Value,
    pub capability_gaps: Vec<String>,
}

impl Settings {
    pub fn validate(&self) -> Result<(), Failure> {
        if !(1..=5).contains(&self.max_review_rounds)
            || !(1..=8).contains(&self.max_tool_rounds)
            || self.max_contract_retries > 3
        {
            return Err(invalid(
                "review rounds must be 1..5, tool rounds 1..8 and contract retries 0..3",
            ));
        }
        unique(self.tools.iter().map(|item| item.name.as_str()))?;
        unique(self.skills.iter().map(|item| item.name.as_str()))?;
        unique(self.profile_checks.iter().map(|item| item.id.as_str()))?;
        if !self.environment.is_object()
            || self.tools.iter().any(|item| !item.parameters.is_object())
        {
            return Err(invalid(
                "host-declared environment and tool schemas must be objects",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Failure {
    pub code: String,
    pub message: String,
}

pub fn invalid(message: &str) -> Failure {
    Failure {
        code: "invalid".into(),
        message: message.into(),
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Generated {
    pub text: String,
    pub usage: Value,
}

/// Exact, durable identity of one paid request. It never identifies a new retry.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AttemptContext {
    pub run_id: String,
    pub run_request_digest: String,
    pub phase: String,
    pub generation_key: String,
    pub attempt_id: String,
    pub request_digest: String,
    pub route_digest: String,
}

impl AttemptContext {
    pub fn storage_key(&self) -> String {
        format!(
            "{}/generation-progress/{}",
            self.run_id,
            digest(&json!(self))
        )
    }
}

/// Public stream evidence, separate from validated phase cache and tool receipts.
/// No raw SSE, headers, reasoning, secret values or arbitrary provider fields.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct GenerationProgress {
    pub version: String,
    pub attempt: AttemptContext,
    pub sequence: u64,
    pub public_text: String,
    pub usage: Value,
    pub metadata: Value,
    pub completed: bool,
}

impl GenerationProgress {
    pub fn new(attempt: AttemptContext) -> Self {
        Self {
            version: PROGRESS_VERSION.into(),
            attempt,
            sequence: 0,
            public_text: String::new(),
            usage: json!({}),
            metadata: json!({}),
            completed: false,
        }
    }

    pub fn accept(&mut self, event: &Value) -> Result<(), Failure> {
        match event["kind"].as_str() {
            Some("delta") => {
                let text = event["text"]
                    .as_str()
                    .ok_or_else(|| invalid("provider delta has no public text"))?;
                if self.completed
                    || self.public_text.len().saturating_add(text.len()) > 4 * 1024 * 1024
                {
                    return Err(invalid(
                        "provider delta followed completion or exceeded four MiB",
                    ));
                }
                self.public_text.push_str(text);
            }
            Some("done") => {
                let text = event["text"]
                    .as_str()
                    .ok_or_else(|| invalid("provider completion has no text"))?;
                if self.completed
                    || text.len() > 4 * 1024 * 1024
                    || (!self.public_text.is_empty() && self.public_text != text)
                {
                    return Err(invalid(
                        "provider completion differs from its public deltas",
                    ));
                }
                self.public_text = text.into();
                self.completed = true;
            }
            Some("metadata") => {
                let metadata: Value = serde_json::from_str(
                    event["text"]
                        .as_str()
                        .ok_or_else(|| invalid("provider metadata has no public payload"))?,
                )
                .map_err(|_| invalid("provider metadata is invalid"))?;
                if metadata["version"] != "tobkiri.chat.metadata.v1"
                    || metadata.as_object().is_none_or(|fields| {
                        fields.keys().any(|key| {
                            ![
                                "version",
                                "generation_id",
                                "model",
                                "provider",
                                "finish_reason",
                            ]
                            .contains(&key.as_str())
                        })
                    })
                {
                    return Err(invalid(
                        "provider metadata has an unsupported public schema",
                    ));
                }
                for key in ["generation_id", "model", "provider", "finish_reason"] {
                    if let Some(value) = metadata.get(key) {
                        let text = value
                            .as_str()
                            .ok_or_else(|| invalid("provider identity is invalid"))?;
                        if text.is_empty()
                            || text.len() > 256
                            || text.chars().any(char::is_control)
                            || (key == "generation_id"
                                && (text.len() > 128
                                    || !text
                                        .bytes()
                                        .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))))
                            || (key == "finish_reason" && text != "stop")
                            || self.metadata.get(key).is_some_and(|old| old != value)
                        {
                            return Err(invalid(
                                "provider changed or invalidated its public identity",
                            ));
                        }
                        self.metadata[key] = value.clone();
                    }
                }
            }
            Some("usage") => {
                for (source, target) in [
                    ("prompt-tokens", "input_tokens"),
                    ("completion-tokens", "output_tokens"),
                ] {
                    if let Some(value) = event.get(source).filter(|v| !v.is_null()) {
                        self.merge_counter(target, value)?;
                    }
                }
                if let Some(extra) = event["text"].as_str() {
                    let extra: Value = serde_json::from_str(extra)
                        .map_err(|_| invalid("provider usage metadata is malformed"))?;
                    for key in ["cached_input_tokens", "reasoning_tokens", "total_tokens"] {
                        if let Some(value) = extra.get(key).filter(|v| !v.is_null()) {
                            self.merge_counter(key, value)?;
                        }
                    }
                    if let Some(cost) = extra.get("cost_usd").filter(|v| !v.is_null()) {
                        let text = cost
                            .as_str()
                            .ok_or_else(|| invalid("provider cost receipt is invalid"))?;
                        if sum_decimals(&[text.to_owned()]).is_none()
                            || self.usage.get("cost_usd").is_some_and(|old| old != cost)
                        {
                            return Err(invalid("provider cost receipt changed or is invalid"));
                        }
                        self.usage["cost_usd"] = cost.clone();
                    }
                }
            }
            Some("progress") => (), // Content-free heartbeat; do not persist arbitrary text.
            _ => return Err(invalid("provider stream has no valid completion evidence")),
        }
        Ok(())
    }

    fn merge_counter(&mut self, key: &str, value: &Value) -> Result<(), Failure> {
        let count = value
            .as_u64()
            .ok_or_else(|| invalid("provider usage counter is invalid"))?;
        if self
            .usage
            .get(key)
            .and_then(Value::as_u64)
            .is_some_and(|old| old > count)
        {
            return Err(invalid("provider usage counter decreased"));
        }
        self.usage[key] = json!(count);
        Ok(())
    }

    pub fn next_snapshot(&mut self) -> Result<(), Failure> {
        self.sequence = self
            .sequence
            .checked_add(1)
            .ok_or_else(|| invalid("generation progress sequence exhausted"))?;
        Ok(())
    }

    fn validate_snapshot(&self) -> Result<(), Failure> {
        if self.version != PROGRESS_VERSION
            || !self.metadata.is_object()
            || !self.usage.is_object()
            || self.public_text.len() > 4 * 1024 * 1024
        {
            return Err(invalid(
                "saved generation progress has an invalid public schema",
            ));
        }
        let mut normalized = Self::new(self.attempt.clone());
        let mut metadata = self.metadata.clone();
        metadata["version"] = json!("tobkiri.chat.metadata.v1");
        normalized.accept(&json!({"kind":"metadata","text":metadata.to_string()}))?;
        normalized.accept(&json!({"kind":"delta","text":self.public_text}))?;
        normalized.accept(
            &json!({"kind":"usage","prompt-tokens":self.usage["input_tokens"],
            "completion-tokens":self.usage["output_tokens"],"text":json!({
                "cached_input_tokens":self.usage["cached_input_tokens"],
                "reasoning_tokens":self.usage["reasoning_tokens"],
                "total_tokens":self.usage["total_tokens"],"cost_usd":self.usage["cost_usd"]
            }).to_string()}),
        )?;
        if normalized.metadata != self.metadata || normalized.usage != self.usage {
            return Err(invalid(
                "saved generation progress contains unsupported fields",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CachedGeneration {
    pub request_digest: String,
    pub generated: Generated,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Phase {
    pub id: String,
    pub extra: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ToolReceipt {
    pub request_digest: String,
    pub status: String,
    pub outcome: Option<Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ResumePoint {
    pub pending: VecDeque<Phase>,
    pub goal: Value,
    pub context: BTreeMap<String, Value>,
    pub sections: Vec<Value>,
    pub candidate: String,
    pub candidate_state: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GenerationPoint {
    pub base_key: String,
    pub key: String,
}

/// Effects are intentionally absent from the pure scheduler implementation.
pub trait Ports {
    fn active(&mut self) -> Result<(), Failure>;
    fn generate(
        &mut self,
        model: &str,
        messages: &[Value],
        phase: &str,
        attempt: &AttemptContext,
    ) -> Result<Generated, Failure>;
    fn generation_progress(
        &mut self,
        _attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        Ok(None)
    }
    fn tool(&mut self, call: &Value) -> Result<Value, Failure>;
    fn verify_artifact(&mut self, path: &str) -> Result<Value, Failure>;
    fn persist(&mut self, run: &Run) -> Result<(), Failure>;
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub version: String,
    pub id: String,
    pub conversation: String,
    pub request_digest: String,
    pub model: String,
    pub messages: Vec<Value>,
    pub status: String,
    pub goal: Value,
    pub context: BTreeMap<String, Value>,
    pub pending: VecDeque<Phase>,
    pub phases: Vec<Value>,
    pub generations: BTreeMap<String, CachedGeneration>,
    #[serde(default)]
    pub rejected_generations: BTreeMap<String, CachedGeneration>,
    #[serde(default)]
    pub contract_retries: BTreeMap<String, u32>,
    pub phase_inputs: BTreeMap<String, Vec<Value>>,
    pub attempts: BTreeMap<String, Value>,
    pub tool_receipts: BTreeMap<String, ToolReceipt>,
    pub artifacts: BTreeMap<String, Value>,
    pub candidate: String,
    pub candidate_state: Value,
    pub sections: Vec<Value>,
    pub review_rounds: u32,
    pub error: Option<Failure>,
    pub settings: Settings,
    #[serde(default)]
    pub phase_epochs: BTreeMap<String, u32>,
    #[serde(default)]
    pub answers: Vec<String>,
    #[serde(default)]
    pub entry: Option<ResumePoint>,
    #[serde(default)]
    pub waiting: Option<ResumePoint>,
    #[serde(default)]
    pub generation_epochs: BTreeMap<String, u32>,
    #[serde(default)]
    pub retry_contexts: BTreeMap<String, Value>,
    #[serde(default)]
    pub last_generation: Option<GenerationPoint>,
}

pub fn digest(value: &Value) -> String {
    // serde_json's default object map is sorted; no floats enter our documents.
    format!("{:x}", Sha256::digest(value.to_string().as_bytes()))
}

fn unique<'a>(ids: impl Iterator<Item = &'a str>) -> Result<(), Failure> {
    let mut seen = BTreeSet::new();
    for id in ids {
        if id.is_empty() || !seen.insert(id) {
            return Err(invalid("ids must be nonempty and unique"));
        }
    }
    Ok(())
}

fn texts(value: &Value, key: &str) -> Vec<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .map(String::from)
        .collect()
}

fn clarification_message(answer: &str) -> Value {
    json!({"role":"user","content":format!("回答（既存Goalの未確認事項に対する補足）: {answer}")})
}

fn checked_ids(value: &Value, key: &str) -> Result<BTreeSet<String>, Failure> {
    let items = value[key]
        .as_array()
        .ok_or_else(|| invalid("missing identity list"))?;
    unique(items.iter().filter_map(|item| item["id"].as_str()))?;
    let mut ids = BTreeSet::new();
    for item in items {
        let id = item["id"]
            .as_str()
            .ok_or_else(|| invalid("missing item id"))?;
        if !id.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
            || !id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
        {
            return Err(invalid(
                "ids must use portable letters, digits, underscores or hyphens",
            ));
        }
        ids.insert(id.into());
    }
    Ok(ids)
}

/// Require strict booleans and exactly one successful check for every Goal id.
pub fn review_approved(review: &Value, candidate: &Value, expected: &BTreeSet<String>) -> bool {
    if review.get("approved") != Some(&Value::Bool(true))
        || !texts(review, "blocking_issues").is_empty()
        || !texts(candidate, "open_issues").is_empty()
    {
        return false;
    }
    let Some(checks) = review["acceptance_results"].as_array() else {
        return false;
    };
    let mut actual = BTreeSet::new();
    for check in checks {
        let Some(id) = check["criterion_id"].as_str() else {
            return false;
        };
        if !actual.insert(id.to_string())
            || check["passed"] != true
            || check["evidence"]
                .as_str()
                .is_none_or(|text| text.trim().is_empty())
        {
            return false;
        }
    }
    !expected.is_empty() && &actual == expected
}

impl Run {
    /// Detect a matching v4 checkpoint before deriving a fresh v5 run. A version
    /// change must not turn an unresolved paid request into a new implicit POST.
    pub fn previous_v4_storage_id(&self) -> String {
        digest(
            &json!({"version":"deepthink.workflow.v4","conversation":self.conversation,
            "model":self.model,"messages":self.messages,"settings":self.settings}),
        )
    }

    pub fn previous_v4_conflict(&self, previous: &Self) -> Failure {
        let expected = self.previous_v4_storage_id();
        if previous.version != "deepthink.workflow.v4"
            || previous.id != expected
            || previous.request_digest != expected
            || previous.conversation != self.conversation
            || previous.model != self.model
            || previous.messages != self.messages
            || digest(&json!(previous.settings)) != digest(&json!(self.settings))
        {
            return invalid("the prior workflow checkpoint does not match its exact request");
        }
        if let Err(error) = previous.ensure_generations_reconciled() {
            return error;
        }
        Failure {
            code: "conflict".into(),
            message: "A v4 workflow already owns this exact input. It was not migrated or repeated. Inspect the original run; an intentionally new request requires a new conversation.".into(),
        }
    }

    fn save(&self, ports: &mut impl Ports) -> Result<(), Failure> {
        ports.persist(self).map_err(|error| Failure {
            code: "checkpoint-unconfirmed".into(),
            message: format!(
                "Checkpoint write was not confirmed; do not automatically repeat the request. {}",
                error.message
            ),
        })
    }
    pub fn new(
        conversation: String,
        model: String,
        messages: Vec<Value>,
        settings: Settings,
    ) -> Result<Self, Failure> {
        settings.validate()?;
        if conversation.is_empty()
            || model.is_empty()
            || messages.is_empty()
            || messages
                .iter()
                .any(|item| item["content"].as_str().is_none())
        {
            return Err(invalid(
                "conversation, exact model and text messages are required",
            ));
        }
        let binding = digest(
            &json!({"version":VERSION,"conversation":conversation,"model":model,"messages":messages,"settings":settings}),
        );
        Ok(Self {
            version: VERSION.into(),
            id: binding.clone(),
            conversation,
            request_digest: binding,
            model,
            messages,
            status: "running".into(),
            goal: json!({"status":"defining","assumptions":[],"clarifications":[]}),
            context: BTreeMap::new(),
            pending: VecDeque::from([Phase {
                id: "goal_intake".into(),
                extra: json!({}),
            }]),
            phases: vec![],
            generations: BTreeMap::new(),
            rejected_generations: BTreeMap::new(),
            contract_retries: BTreeMap::new(),
            phase_inputs: BTreeMap::new(),
            attempts: BTreeMap::new(),
            tool_receipts: BTreeMap::new(),
            artifacts: BTreeMap::new(),
            candidate: String::new(),
            candidate_state: Value::Null,
            sections: vec![],
            review_rounds: 0,
            error: None,
            settings,
            phase_epochs: BTreeMap::new(),
            answers: vec![],
            entry: None,
            waiting: None,
            generation_epochs: BTreeMap::new(),
            retry_contexts: BTreeMap::new(),
            last_generation: None,
        })
    }

    pub fn resume(&mut self, expected: &Self) -> Result<(), Failure> {
        self.ensure_generations_reconciled()?;
        if self.version != VERSION || self.request_digest != expected.request_digest {
            return Err(invalid(
                "resume cannot change the exact model, input or captured configuration",
            ));
        }
        if self
            .tool_receipts
            .values()
            .any(|receipt| receipt.status != "completed")
        {
            return Err(Failure {code:"conflict".into(),message:"A tool outcome is unresolved; reconcile it before resuming. No tool was automatically replayed.".into()});
        }
        if self.error.as_ref().is_some_and(|error| {
            matches!(
                error.code.as_str(),
                "checkpoint-unconfirmed" | "core-internal"
            )
        }) {
            return Err(Failure {
                code: "needs-reconciliation".into(),
                message: "An unconfirmed checkpoint or Core operation cannot be resumed automatically. Inspect its original receipt before continuing.".into(),
            });
        }
        if self.status == "paused" {
            self.pause_receipt()?;
            self.status = "running".into();
        }
        if self.status == "quarantined"
            && self
                .error
                .as_ref()
                .is_some_and(|error| error.code != "review-rejected")
        {
            if let Some(point) = self.last_generation.clone() {
                if self.generations.contains_key(&point.key)
                    || self.rejected_generations.contains_key(&point.key)
                {
                    let prefix = format!("{}:", point.base_key);
                    if self.tool_receipts.iter().any(|(key, receipt)| {
                        key.starts_with(&prefix) && receipt.status == "completed"
                    }) {
                        return Err(Failure{code:"conflict".into(),message:"The failed generation already committed tool effects. Its output cannot be safely regenerated automatically; reconcile the retained receipts first.".into()});
                    }
                    let next = self
                        .generation_epochs
                        .get(&point.base_key)
                        .copied()
                        .unwrap_or(0)
                        .checked_add(1)
                        .ok_or_else(|| invalid("generation retry references are exhausted"))?;
                    self.generation_epochs.insert(point.base_key.clone(), next);
                    let phase_prefix = format!("{}:", point.base_key.rsplit_once(':').unwrap().0);
                    let allow_additional_tools =
                        !self.tool_receipts.iter().any(|(key, receipt)| {
                            key.starts_with(&phase_prefix) && receipt.status == "completed"
                        });
                    self.retry_contexts.insert(format!("{}#{next}",point.base_key),json!({
                        "failed_generation":point.key,"failure":self.error,
                        "completed_tool_receipts":self.tool_receipts,
                        "allow_additional_tools":allow_additional_tools,
                        "instruction":"Correct only the failed generation. Earlier tool effects are already complete and must not be repeated. Return the required phase contract using the retained evidence."
                    }));
                }
            }
            if let Some(point) = self.entry.clone() {
                self.pending = point.pending;
                self.goal = point.goal;
                self.context = point.context;
                self.sections = point.sections;
                self.candidate = point.candidate;
                self.candidate_state = point.candidate_state;
            }
            self.status = "running".into();
            self.error = None;
        }
        Ok(())
    }

    /// Pending legacy attempts and completed receipts without their exact public
    /// generation are both unresolved. No continuation may create a fresh POST.
    pub fn ensure_generations_reconciled(&self) -> Result<(), Failure> {
        if self.attempts.iter().any(|(id, attempt)| {
            let key = attempt
                .pointer("/context/generation_key")
                .and_then(Value::as_str)
                .or_else(|| id.rsplit_once(':').map(|(key, _)| key));
            let record = key.and_then(|key| {
                self.generations
                    .get(key)
                    .or_else(|| self.rejected_generations.get(key))
            });
            attempt["status"] != "completed"
                || record.is_none()
                || attempt
                    .pointer("/context/request_digest")
                    .and_then(Value::as_str)
                    .is_some_and(|expected| {
                        record.is_none_or(|record| record.request_digest != expected)
                    })
        }) {
            return Err(Failure { code:"needs-reconciliation".into(), message:
                "A model attempt has no durably finalized generation. Inspect its retained provider id, public partial and usage; reconcile it explicitly before continuing. No model request was automatically repeated.".into() });
        }
        Ok(())
    }

    pub fn merge_generation_progress(
        &mut self,
        progress: GenerationProgress,
    ) -> Result<(), Failure> {
        progress.validate_snapshot()?;
        let attempt = self
            .attempts
            .get_mut(&progress.attempt.attempt_id)
            .ok_or_else(|| invalid("generation progress has no original attempt"))?;
        if progress.version != PROGRESS_VERSION
            || progress.attempt.run_id != self.id
            || progress.attempt.run_request_digest != self.request_digest
            || attempt.get("context") != Some(&json!(progress.attempt))
        {
            return Err(invalid(
                "generation progress changed its pinned request or route",
            ));
        }
        if let Some(previous) = attempt.get("progress") {
            let previous: GenerationProgress = serde_json::from_value(previous.clone())
                .map_err(|_| invalid("saved generation progress is invalid"))?;
            if progress.sequence < previous.sequence
                || !progress.public_text.starts_with(&previous.public_text)
                || (previous.completed && !progress.completed)
                || (progress.sequence == previous.sequence && progress != previous)
            {
                return Err(invalid("generation progress regressed"));
            }
            for (key, value) in previous
                .usage
                .as_object()
                .ok_or_else(|| invalid("saved usage is invalid"))?
            {
                let current = progress.usage.get(key);
                if current.is_none()
                    || (key == "cost_usd" && current != Some(value))
                    || value.as_u64().is_some_and(|old| {
                        current.and_then(Value::as_u64).is_none_or(|new| new < old)
                    })
                {
                    return Err(invalid("generation progress usage regressed"));
                }
            }
            for (key, value) in previous
                .metadata
                .as_object()
                .ok_or_else(|| invalid("saved metadata is invalid"))?
            {
                if progress.metadata.get(key) != Some(value) {
                    return Err(invalid("generation progress identity changed"));
                }
            }
        }
        attempt["usage"] = progress.usage.clone();
        attempt["progress"] = json!(progress);
        Ok(())
    }

    /// Explicit references resume the original input, irrespective of later
    /// conversation-history messages. The model and captured config stay pinned.
    pub fn resume_pinned(
        &mut self,
        conversation: &str,
        model: &str,
        settings: &Settings,
    ) -> Result<(), Failure> {
        if self.conversation != conversation
            || self.model != model
            || digest(&json!(self.settings)) != digest(&json!(settings))
        {
            return Err(invalid(
                "continuation changed its conversation, exact model or captured configuration",
            ));
        }
        let expected = self.clone();
        self.resume(&expected)
    }

    /// Preserve the original user task in the eventual conversation turn. A
    /// proposed clarification is saved only if the continuation Flow succeeds.
    pub fn conversation_input(&self, proposed_answer: Option<&str>) -> Result<String, Failure> {
        let initial_length = self
            .messages
            .len()
            .checked_sub(self.answers.len())
            .ok_or_else(|| invalid("clarification history has no original request"))?;
        if self.messages[initial_length..]
            .iter()
            .zip(&self.answers)
            .any(|(message, answer)| *message != clarification_message(answer))
        {
            return Err(invalid(
                "saved clarification messages do not match accepted answers",
            ));
        }
        let mut text = self.messages[..initial_length]
            .iter()
            .rev()
            .find(|message| message["role"] == "user")
            .and_then(|message| message["content"].as_str())
            .ok_or_else(|| invalid("the workflow has no original user task"))?
            .to_owned();
        let mut append_answer = |answer: &str| {
            text.push_str("\n\n確認事項への回答:\n");
            text.push_str(answer);
        };
        for answer in &self.answers {
            append_answer(answer);
        }
        if let Some(answer) = proposed_answer {
            if self.status != "needs_input" || answer.trim().is_empty() {
                return Err(invalid(
                    "a proposed answer requires an unresolved user question",
                ));
            }
            append_answer(answer);
        }
        Ok(text)
    }

    /// Apply an explicit answer without rewriting the original request or costs.
    pub fn answer(
        &mut self,
        conversation: &str,
        answer: &str,
        settings: &Settings,
    ) -> Result<(), Failure> {
        self.resume_pinned(conversation, &self.model.clone(), settings)?;
        if self.status != "needs_input" || answer.trim().is_empty() {
            return Err(invalid(
                "a nonempty answer requires a workflow waiting for user input",
            ));
        }
        let point = self
            .waiting
            .take()
            .ok_or_else(|| invalid("the paused phase has no continuation checkpoint"))?;
        let prior_goal = self.goal.clone();
        let current = point
            .pending
            .front()
            .ok_or_else(|| invalid("continuation has no paused phase"))?
            .id
            .clone();
        self.goal = point.goal;
        self.goal["clarifications"] = json!([]);
        self.context = point.context;
        self.context.insert("clarification_answer".into(),json!({"questions":prior_goal["clarifications"],"answer":answer,"prior_goal":prior_goal}));
        self.pending = point.pending;
        self.sections = point.sections;
        self.candidate = point.candidate;
        self.candidate_state = point.candidate_state;
        self.answers.push(answer.to_string());
        self.messages.push(clarification_message(answer));
        *self.phase_epochs.entry(current).or_default() += 1;
        self.entry = None;
        self.error = None;
        self.status = "running".into();
        Ok(())
    }

    fn push(&mut self, id: &str, extra: Value) {
        self.pending.push_back(Phase {
            id: id.into(),
            extra,
        });
    }

    fn needs_input(&mut self, questions: Vec<String>) {
        self.status = "needs_input".into();
        self.goal["status"] = json!("needs_input");
        self.goal["clarifications"] = json!(questions);
        self.waiting = self.entry.clone();
    }

    fn phase_context(&self, phase: &Phase) -> Value {
        let mut context = json!({"goal":self.goal,"model":self.model,
            "environment":self.settings.environment,"capability_gaps":self.settings.capability_gaps,
            "artifact_directory":"DeepThink private checkpoint store; not a physical workspace",
            "tool_workspace":"Only locations explicitly provided by the selected real tool ports"});
        context["user_answers"] = json!(self.answers);
        context["verified_artifacts"] = json!(self.artifacts);
        context["tool_execution_receipts"] = json!(self.tool_receipts);
        for (key, value) in &self.context {
            context[key] = value.clone();
        }
        let base = phase.id.split(':').next().unwrap();
        if matches!(base, "capability_assessment" | "means_selection")
            || self.context.contains_key("selection")
        {
            let selected = self.context.get("selection");
            context["tool_inventory"] = json!(self.settings.tools.iter().filter(|tool| {
                selected.is_none_or(|selection| texts(selection,"selected_tools").contains(&tool.name))
            }).map(|tool|json!({"name":tool.name,"description":tool.description.chars().take(500).collect::<String>(),"parameters":tool.parameters})).collect::<Vec<_>>());
            context["skill_inventory"] = json!(self.settings.skills.iter().filter(|skill| {
                selected.is_none_or(|selection| texts(selection,"selected_skills").contains(&skill.name))
            }).map(|skill| if selected.is_some() {json!(skill)} else {json!({"name":skill.name,"description":skill.description.chars().take(500).collect::<String>()})}).collect::<Vec<_>>());
        }
        for (key, value) in phase.extra.as_object().unwrap() {
            context[key] = value.clone();
        }
        if base == "section" {
            context["completed_sections"] = json!(self.sections);
        }
        if matches!(base, "review" | "revision") {
            context["candidate"] = json!(self.candidate);
            context["candidate_state"] = self.candidate_state.clone();
        }
        context
    }

    fn phase_messages(&self, phase: &Phase) -> Vec<Value> {
        let mut messages = vec![json!({"role":"system","content":
            "You perform one explicit workflow phase. Return exactly one JSON object matching the schema, with nothing before or after it. The three top-level keys summary, tool_calls and result are mandatory inside that same object; always include tool_calls:[] even when no tools are needed. Never append a missing field after closing the object. summary contains concise decisions, assumptions and next actions, never private reasoning or chain-of-thought. All tool execution remains subject to the host's Gate, permissions and approvals. Never invent tools, skills, sources, files, completed checks or secret values. Use tool_calls only for actual selected tools; set result=null while requesting tools. Otherwise tool_calls=[] and result is the phase result. Do not request a user answer for mere notes, unused tools or confirmed settings."})];
        messages.extend(self.messages.clone());
        messages.push(json!({"role":"user","content":format!("{}\n\nphase:{}\n\nschema:{}\n\ncontext:{}",phases::prompt(&phase.id),phase.id,phases::schema(&phase.id),self.phase_context(phase))}));
        messages
    }

    fn generate_phase(&mut self, ports: &mut impl Ports, phase: &Phase) -> Result<Value, Failure> {
        self.ensure_generations_reconciled()?;
        if self
            .tool_receipts
            .values()
            .any(|receipt| receipt.status != "completed")
        {
            return Err(Failure {
                code: "conflict".into(),
                message: "Pending tool outcome needs reconciliation; it will not be replayed."
                    .into(),
            });
        }
        let phase_key = format!(
            "{}@{}",
            phase.id,
            self.phase_epochs.get(&phase.id).copied().unwrap_or(0)
        );
        let mut messages = if let Some(saved) = self.phase_inputs.get(&phase_key) {
            saved.clone()
        } else {
            let messages = self.phase_messages(phase);
            self.phase_inputs
                .insert(phase_key.clone(), messages.clone());
            self.save(ports)?;
            messages
        };
        for round in 0..=self.settings.max_tool_rounds {
            ports.active()?;
            let base_key = format!("{phase_key}:{round}");
            let revision = self.generation_epochs.get(&base_key).copied().unwrap_or(0);
            let key = format!("{base_key}#{revision}");
            if let Some(retry) = self.retry_contexts.get(&key) {
                messages.push(json!({"role":"user","content":format!("Retry the incomplete final generation only. Retained diagnostic and real-effect evidence: {retry}")}));
            }
            self.last_generation = Some(GenerationPoint {
                base_key,
                key: key.clone(),
            });
            let request_digest = digest(
                &json!({"model":self.model,"messages":messages,"schema":phases::schema(&phase.id)}),
            );
            let cached = self.generations.contains_key(&key);
            let generated = if let Some(cached) = self.generations.get(&key) {
                if cached.request_digest != request_digest {
                    return Err(invalid(
                        "completed generation request changed during resume",
                    ));
                }
                cached.generated.clone()
            } else {
                // A pending generation remains durable even if the transport
                // stops before it can return a usage receipt.
                let attempt = format!("{}:{}", key, self.attempts.len());
                let context = AttemptContext {
                    run_id: self.id.clone(),
                    run_request_digest: self.request_digest.clone(),
                    phase: phase.id.clone(),
                    generation_key: key.clone(),
                    attempt_id: attempt.clone(),
                    request_digest: request_digest.clone(),
                    route_digest: digest(&json!({"model":self.model,"settings":self.settings})),
                };
                self.attempts.insert(
                    attempt.clone(),
                    json!({"phase":phase.id,"status":"pending","usage":null,"context":context}),
                );
                self.save(ports)?;
                let outcome = ports.generate(&self.model, &messages, &phase.id, &context);
                if let Err(error) = &outcome
                    && error.code == "checkpoint-unconfirmed"
                {
                    return Err(error.clone());
                }
                let progress = ports.generation_progress(&context);
                match outcome {
                    Ok(generated) => {
                        if let Some(progress) = progress? {
                            self.merge_generation_progress(progress)?;
                        }
                        generated
                    }
                    Err(error) => {
                        // A cancelled/deadline context may no longer admit a
                        // sidecar read. Preserve the original transport/unknown
                        // classification; already committed sidecars survive.
                        if let Ok(Some(progress)) = progress {
                            if self.merge_generation_progress(progress).is_err() {
                                return Err(Failure {
                                    code: error.code,
                                    message: format!(
                                        "{} Retained progress could not be verified; reconciliation is required.",
                                        error.message
                                    ),
                                });
                            }
                        }
                        return Err(error);
                    }
                }
            };
            let validated = serde_json::from_str::<Value>(&generated.text)
                .map_err(|_| Failure {
                    code: "invalid-phase-contract".into(),
                    message: "generation returned malformed JSON".into(),
                })
                .and_then(|value| {
                    phases::validate(&value, &phases::schema(&phase.id)).map_err(|message| {
                        Failure {
                            code: "invalid-phase-contract".into(),
                            message,
                        }
                    })?;
                    Ok(value)
                });
            let value = match validated {
                Ok(value) => value,
                Err(error) => {
                    // Public completion text and its actual receipt survive a
                    // failed contract. Rejected output is never a cache hit.
                    self.generations.remove(&key);
                    self.complete_attempt(&key, &request_digest, &generated.usage)?;
                    self.rejected_generations.insert(
                        key,
                        CachedGeneration {
                            request_digest,
                            generated,
                        },
                    );
                    // This is a complete recovery checkpoint even if the host
                    // stops before advance() can persist its outer error path.
                    self.status = "quarantined".into();
                    self.goal["status"] = json!("quarantined");
                    self.error = Some(error.clone());
                    self.save(ports)?;
                    return Err(error);
                }
            };
            if !cached {
                self.complete_attempt(&key, &request_digest, &generated.usage)?;
                self.generations.insert(
                    key.clone(),
                    CachedGeneration {
                        request_digest,
                        generated: generated.clone(),
                    },
                );
                self.save(ports)?;
            }
            let calls = value["tool_calls"].as_array().unwrap();
            if !calls.is_empty()
                && self
                    .retry_contexts
                    .get(&key)
                    .is_some_and(|retry| retry["allow_additional_tools"] == false)
            {
                return Err(Failure{code:"conflict".into(),message:"This correction follows completed tool effects. Additional tool requests require reconciliation; retained effects were not repeated.".into()});
            }
            if calls.is_empty() {
                if value["result"].is_null() {
                    return Err(invalid("completed phase has no result"));
                }
                self.context
                    .insert("last_summary".into(), value["summary"].clone());
                return Ok(value["result"].clone());
            }
            if !value["result"].is_null() || round == self.settings.max_tool_rounds {
                return Err(invalid(
                    "tool phase is mixed with a final result or its round limit was exhausted",
                ));
            }
            if !matches!(
                phase.id.split(':').next().unwrap(),
                "work_environment"
                    | "reference_search"
                    | "evidence"
                    | "section"
                    | "integration"
                    | "review"
                    | "revision"
            ) {
                return Err(invalid("this declarative phase does not execute tools"));
            }
            let selected = self
                .context
                .get("selection")
                .map(|selection| texts(selection, "selected_tools"))
                .unwrap_or_default();
            let mut outcomes = Vec::new();
            unique(calls.iter().filter_map(|call| call["id"].as_str()))?;
            for (index, call) in calls.iter().enumerate() {
                ports.active()?;
                let name = call["name"].as_str().unwrap();
                if !selected.iter().any(|selected| selected == name) {
                    return Err(invalid("model requested an unavailable or unselected tool"));
                }
                let definition = self
                    .settings
                    .tools
                    .iter()
                    .find(|tool| tool.name == name)
                    .ok_or_else(|| invalid("tool inventory changed"))?;
                phases::validate(&call["arguments"], &definition.parameters)
                    .map_err(|_| invalid("tool arguments violate their declared schema"))?;
                let receipt_key = format!("{phase_key}:{round}:{index}");
                let tool_id = digest(&json!({"run":self.id,"position":receipt_key}));
                let exact_call = json!({"id":tool_id,"name":name,"arguments":call["arguments"]});
                let call_digest = digest(&exact_call);
                let outcome = if let Some(receipt) = self.tool_receipts.get(&receipt_key) {
                    if receipt.request_digest != call_digest || receipt.status != "completed" {
                        return Err(invalid("tool receipt is changed or unresolved"));
                    }
                    receipt
                        .outcome
                        .clone()
                        .ok_or_else(|| invalid("missing tool receipt outcome"))?
                } else {
                    self.tool_receipts.insert(
                        receipt_key.clone(),
                        ToolReceipt {
                            request_digest: call_digest,
                            status: "pending".into(),
                            outcome: None,
                        },
                    );
                    self.save(ports)?;
                    let outcome = ports.tool(&exact_call)?;
                    if outcome.get("ok") != Some(&Value::Bool(true)) {
                        return Err(Failure {code:"blocked".into(),message:"Real tool did not return success; its pending receipt was retained for reconciliation.".into()});
                    }
                    let receipt = self.tool_receipts.get_mut(&receipt_key).unwrap();
                    receipt.status = "completed".into();
                    receipt.outcome = Some(outcome.clone());
                    self.save(ports)?;
                    outcome
                };
                outcomes.push(json!({"id":call["id"],"name":name,"outcome":outcome}));
            }
            messages.push(json!({"role":"assistant","content":generated.text}));
            messages.push(json!({"role":"user","content":format!("Verified real tool results: {}. Continue the SAME phase; return its final contract or request another permitted tool.",json!(outcomes))}));
        }
        Err(invalid("tool loop ended without a completed phase"))
    }

    fn complete_attempt(
        &mut self,
        key: &str,
        request_digest: &str,
        usage: &Value,
    ) -> Result<(), Failure> {
        let attempt = self
            .attempts
            .values_mut()
            .find(|attempt| {
                attempt
                    .pointer("/context/generation_key")
                    .and_then(Value::as_str)
                    == Some(key)
                    && attempt
                        .pointer("/context/request_digest")
                        .and_then(Value::as_str)
                        == Some(request_digest)
            })
            .ok_or_else(|| invalid("completed generation has no exact attempt"))?;
        attempt["status"] = json!("completed");
        attempt["usage"] = usage.clone();
        Ok(())
    }

    fn verify(&mut self, ports: &mut impl Ports, value: &Value) -> Result<(), Failure> {
        for path in texts(value, "artifact_paths") {
            if path.starts_with('/')
                || path.contains('\\')
                || path.contains(':')
                || path
                    .split('/')
                    .any(|part| part.is_empty() || part == "." || part == "..")
            {
                return Err(invalid(
                    "artifact path must remain a portable relative tool-owned path",
                ));
            }
            let reference = ports.verify_artifact(&path)?;
            self.artifacts.insert(path, reference);
        }
        Ok(())
    }

    fn retry_rejected_contract(
        &mut self,
        ports: &mut impl Ports,
        phase: &Phase,
        error: &Failure,
    ) -> Result<Option<Value>, Failure> {
        let Some(point) = self.last_generation.clone() else {
            return Ok(None);
        };
        if error.code != "invalid-phase-contract"
            || !self.rejected_generations.contains_key(&point.key)
        {
            return Ok(None);
        }
        let count = self
            .contract_retries
            .get(&point.base_key)
            .copied()
            .unwrap_or(0);
        if count >= self.settings.max_contract_retries {
            return Ok(None);
        }
        // An earlier tool round in this phase may already have committed an
        // effect. Such phases require explicit reconciliation/resume instead.
        let phase_prefix = format!("{}:", point.base_key.rsplit_once(':').unwrap().0);
        if self
            .tool_receipts
            .iter()
            .any(|(key, receipt)| key.starts_with(&phase_prefix) || receipt.status != "completed")
        {
            return Ok(None);
        }
        let expected = self.clone();
        self.resume(&expected)?;
        self.contract_retries.insert(point.base_key, count + 1);
        self.save(ports)?;
        Ok(Some(json!({
            "version":VERSION,"run_id":self.id,"phase":phase.id,"status":"retrying",
            "reason":"invalid_phase_contract","retry":count+1,
            "maximum_retries":self.settings.max_contract_retries,"usage":self.usage()
        })))
    }

    /// Perform at most one domain phase and persist every completed transition.
    pub fn advance(&mut self, ports: &mut impl Ports) -> Result<Vec<Value>, Failure> {
        self.advance_with_boundary(ports, false)
    }

    /// The public stream invocation has a fixed budget of one completed phase.
    /// Contract retries do not spend that budget; they never renew its deadline.
    pub fn advance_segment(&mut self, ports: &mut impl Ports) -> Result<Vec<Value>, Failure> {
        self.advance_with_boundary(ports, true)
    }

    /// A segment boundary is a transport suspension, never an approved answer.
    pub fn pause_receipt(&self) -> Result<Value, Failure> {
        if self.version != VERSION || self.status != "paused" {
            return Err(invalid("the workflow is not a supported paused segment"));
        }
        self.ensure_phase_boundary()?;
        if self.entry.is_some()
            || self
                .phases
                .last()
                .is_none_or(|phase| phase["status"] != "completed")
        {
            return Err(invalid(
                "a paused workflow has no completed phase checkpoint",
            ));
        }
        Ok(
            json!({"version":VERSION,"run_id":self.id,"conversation":self.conversation,
            "status":"paused","approved":false,"next_phase":self.pending.front().map(|phase|phase.id.as_str()),
            "completed_phases":self.phases.len(),"usage":self.usage()}),
        )
    }

    fn ensure_phase_boundary(&self) -> Result<(), Failure> {
        self.ensure_generations_reconciled()?;
        if self.error.is_some()
            || self.waiting.is_some()
            || self.pending.is_empty()
            || self
                .tool_receipts
                .values()
                .any(|receipt| receipt.status != "completed" || receipt.outcome.is_none())
        {
            return Err(Failure {
                code: "needs-reconciliation".into(),
                message: "Only a settled completed phase can be suspended or continued. Uncertain model, tool or checkpoint work was retained and not repeated.".into(),
            });
        }
        Ok(())
    }

    fn advance_with_boundary(
        &mut self,
        ports: &mut impl Ports,
        suspend_after_phase: bool,
    ) -> Result<Vec<Value>, Failure> {
        if self.status != "running" {
            return Ok(vec![]);
        }
        let phase = self
            .pending
            .front()
            .cloned()
            .ok_or_else(|| invalid("running workflow has no phase"))?;
        self.last_generation = None;
        if self.entry.is_none() {
            self.entry = Some(ResumePoint {
                pending: self.pending.clone(),
                goal: self.goal.clone(),
                context: self.context.clone(),
                sections: self.sections.clone(),
                candidate: self.candidate.clone(),
                candidate_state: self.candidate_state.clone(),
            });
            self.save(ports)?;
        }
        let outcome = self.advance_inner(ports, &phase).and_then(|value| {
            if suspend_after_phase && self.status == "running" {
                self.ensure_phase_boundary()?;
            }
            Ok(value)
        });
        match outcome {
            Ok(value) => {
                self.phases.push(json!({"phase":phase.id,"status":"completed","summary":self.context.get("last_summary"),"result":value}));
                self.entry = None;
                if suspend_after_phase && self.status == "running" {
                    // Commit the phase transition and paused state together.
                    // There is no second pause write or in-flight context reset.
                    self.status = "paused".into();
                }
                if let Err(error) = self.save(ports) {
                    self.status = "quarantined".into();
                    self.goal["status"] = json!("quarantined");
                    self.error = Some(error.clone());
                    // Do not publish a pause/completion whose final checkpoint
                    // was not acknowledged, and never issue a replacement write.
                    return Err(error);
                }
                Ok(vec![
                    json!({"version":VERSION,"run_id":self.id,"phase":phase.id,"status":"completed","summary":self.context.get("last_summary"),"usage":self.usage()}),
                ])
            }
            Err(error) => {
                self.status = "quarantined".into();
                self.goal["status"] = json!("quarantined");
                self.error = Some(error.clone());
                if matches!(
                    error.code.as_str(),
                    "checkpoint-unconfirmed" | "core-internal"
                ) {
                    // The original write may still own its worker/transaction.
                    // Do not issue another write or retry from an older snapshot.
                    return Err(error);
                }
                if let Some(progress) = self.retry_rejected_contract(ports, &phase, &error)? {
                    return Ok(vec![progress]);
                }
                self.save(ports)?;
                Err(error)
            }
        }
    }

    fn advance_inner(&mut self, ports: &mut impl Ports, phase: &Phase) -> Result<Value, Failure> {
        let value = self.generate_phase(ports, phase)?;
        let base = phase.id.split(':').next().unwrap();
        let mut questions = texts(&value, "clarifications");
        match base {
            "goal_intake" => {
                checked_ids(&value, "requirements")?;
                checked_ids(&value, "deliverables")?;
                self.goal = value.clone();
                self.goal["status"] = json!("defining");
                for (index, term) in value["quality_terms"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .enumerate()
                {
                    self.push(
                        &format!("quality_terms:{}", index + 1),
                        json!({"current_quality_term":term,"term_index":index}),
                    );
                }
                self.push("goal_contract", json!({}));
                if value["quality_terms"].as_array().unwrap().is_empty() && !questions.is_empty() {
                    self.needs_input(questions.clone());
                }
                questions.clear();
            }
            "quality_terms" => {
                let index = phase.extra["term_index"].as_u64().unwrap() as usize;
                if value["term"] != phase.extra["current_quality_term"]["term"] {
                    return Err(invalid("quality phase changed its assigned term"));
                }
                self.goal["quality_terms"][index]["definition_result"] = value.clone();
                if value["resolved"] != true
                    || value["metrics"].as_array().unwrap().is_empty()
                    || value["definition"].as_str().unwrap().is_empty()
                {
                    questions.push(format!(
                        "「{}」の測定方法と合格閾値を指定してください。",
                        value["term"].as_str().unwrap()
                    ));
                }
                if index + 1 == self.goal["quality_terms"].as_array().unwrap().len() {
                    questions.extend(texts(&self.goal, "clarifications"));
                }
            }
            "goal_contract" => {
                checked_ids(&value, "success_criteria")?;
                let required = checked_ids(&self.goal, "requirements")?;
                let covered = value["success_criteria"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|item| item["requirement_id"].as_str().unwrap().to_string())
                    .collect::<BTreeSet<_>>();
                if required != covered {
                    if value["confirmed"] == true {
                        return Err(invalid(
                            "Goal contract omitted or invented original requirement criteria; regenerate this contract without changing the Goal",
                        ));
                    }
                    questions.push("元要件すべてに対応する合否基準を補足してください。".into());
                }
                if value["confirmed"] != true {
                    questions.push("未確定のGoal条件を確認してください。".into());
                }
                self.goal["contract"] = value.clone();
                if questions.is_empty() {
                    self.goal["status"] = json!("confirmed");
                    self.goal["clarifications"] = json!([]);
                    self.push("preflight", json!({}));
                }
            }
            "preflight" => {
                questions.extend(texts(&value, "blockers"));
                for check in value["checks"].as_array().unwrap() {
                    if check["passed"] != true {
                        questions.push(check["requirement"].as_str().unwrap().into());
                    }
                }
                self.context.insert(base.into(), value.clone());
                self.push("answer_plan", json!({}));
            }
            "answer_plan" => {
                checked_ids(&value, "sections")?;
                self.context.insert("plan".into(), value.clone());
                for id in [
                    "capability_assessment",
                    "means_selection",
                    "work_environment",
                ] {
                    self.push(id, json!({}));
                }
                for check in self.settings.profile_checks.clone() {
                    self.push(
                        &format!("profile_check:{}", check.id),
                        json!({"current_profile_check":check}),
                    );
                }
                let kind = self.goal["production_kind"]
                    .as_str()
                    .unwrap_or("")
                    .to_ascii_lowercase();
                if kind.contains("3d")
                    || kind.contains("立体")
                    || kind.contains("三次元")
                    || value["reference_search_required"] == true
                {
                    self.push("reference_plan", json!({}));
                    self.push("reference_search", json!({}));
                }
                self.push("evidence", json!({}));
                for section in value["sections"].as_array().unwrap() {
                    self.push(
                        &format!("section:{}", section["id"].as_str().unwrap()),
                        json!({"current_section":section}),
                    );
                }
                self.push("integration", json!({}));
                self.push("review:1", json!({"review_round":1}));
            }
            "capability_assessment" => {
                questions.extend(texts(&value, "blockers"));
                self.context.insert(base.into(), value.clone());
            }
            "means_selection" => {
                for (key, available) in [
                    (
                        "selected_tools",
                        self.settings
                            .tools
                            .iter()
                            .map(|item| item.name.as_str())
                            .collect::<BTreeSet<_>>(),
                    ),
                    (
                        "selected_skills",
                        self.settings
                            .skills
                            .iter()
                            .map(|item| item.name.as_str())
                            .collect(),
                    ),
                ] {
                    unique(texts(&value, key).iter().map(String::as_str))?;
                    if texts(&value, key)
                        .iter()
                        .any(|id| !available.contains(id.as_str()))
                    {
                        return Err(invalid("model selected an unprovided tool or skill"));
                    }
                }
                self.context.insert("selection".into(), value.clone());
                let required = checked_ids(&self.goal, "requirements")?;
                for gap in value["gaps"].as_array().unwrap() {
                    let ids = texts(gap, "requirement_ids");
                    unique(ids.iter().map(String::as_str))?;
                    if ids.iter().any(|id| !required.contains(id))
                        || (gap["blocking"] == true && ids.is_empty())
                    {
                        return Err(invalid(
                            "a capability gap must identify actual Goal requirements",
                        ));
                    }
                    if gap["blocking"] == true {
                        questions.push(format!(
                            "{} {}",
                            gap["description"].as_str().unwrap(),
                            gap["resolution"].as_str().unwrap()
                        ));
                    }
                }
            }
            "work_environment" | "evidence" => {
                if value["ready"] != true {
                    questions.push(
                        "必要な作業・検証環境または根拠を利用できるようにしてください。".into(),
                    );
                }
                self.context.insert(base.into(), value.clone());
            }
            "profile_check" => {
                if phase.extra["current_profile_check"]["required"] == true
                    && value["passed"] != true
                {
                    questions.extend(texts(&value, "findings"));
                    if questions.is_empty() {
                        questions.push("Profile追加工程の必須条件を補足してください。".into());
                    }
                }
                self.context.insert(phase.id.clone(), value.clone());
            }
            "reference_plan" => {
                self.context.insert(base.into(), value.clone());
            }
            "reference_search" => {
                checked_ids(&value, "references")?;
                let selected = texts(&value, "selected_reference_ids");
                if selected.is_empty() {
                    questions.push("制作に使う参照の確認可能なURLを指定してください。".into());
                }
                for id in selected {
                    let reference = value["references"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|item| item["id"] == id)
                        .ok_or_else(|| invalid("selected reference does not exist"))?;
                    let url = reference["source_url"].as_str().unwrap();
                    if !(url.starts_with("https://") || url.starts_with("http://"))
                        || url
                            .split("://")
                            .nth(1)
                            .is_none_or(|host| host.is_empty() || host.starts_with('/'))
                    {
                        questions.push(format!("参照{id}の確認可能なURLを指定してください。"));
                    }
                }
                self.context.insert("references".into(), value.clone());
            }
            "section" => {
                if value["section_id"] != phase.extra["current_section"]["id"] {
                    return Err(invalid("section output changed its assigned id"));
                }
                self.verify(ports, &value)?;
                self.sections.push(value.clone());
            }
            "integration" | "revision" => {
                self.verify(ports, &value)?;
                let kind = self.goal["production_kind"]
                    .as_str()
                    .unwrap_or("")
                    .to_ascii_lowercase();
                if kind.contains("3d") && self.artifacts.is_empty() {
                    return Err(invalid(
                        "3D production requires a verified real artifact; text cannot satisfy it",
                    ));
                }
                self.candidate = value["content"].as_str().unwrap().into();
                self.candidate_state = value.clone();
                if base == "revision" {
                    let round = phase.extra["review_round"].as_u64().unwrap() + 1;
                    self.push(&format!("review:{round}"), json!({"review_round":round}));
                }
            }
            "review" => {
                self.review_rounds = phase.extra["review_round"].as_u64().unwrap() as u32;
                let expected = checked_ids(&self.goal["contract"], "success_criteria")?;
                if review_approved(&value, &self.candidate_state, &expected) {
                    // Re-read owner proofs after all model/tool review activity.
                    for path in self.artifacts.clone().keys() {
                        let proof = ports.verify_artifact(path)?;
                        self.artifacts.insert(path.clone(), proof);
                    }
                    self.status = "completed".into();
                    self.goal["status"] = json!("completed");
                } else if self.review_rounds >= self.settings.max_review_rounds {
                    return Err(Failure {code:"review-rejected".into(),message:"最終レビューが不合格のため候補を隔離しました。Goal・制作物・全チェックポイントは保持されています。".into()});
                } else {
                    self.context.insert("review".into(), value.clone());
                    self.push(
                        &format!("revision:{}", self.review_rounds),
                        json!({"review_round":self.review_rounds,"review":value}),
                    );
                }
            }
            _ => unreachable!(),
        }
        if !questions.is_empty() {
            self.needs_input(questions);
        }
        let assumptions = texts(&value, "assumptions");
        if let Some(current) = self.goal["assumptions"].as_array_mut() {
            for assumption in assumptions {
                let assumption = json!(assumption);
                if !current.contains(&assumption) {
                    current.push(assumption);
                }
            }
        }
        self.pending.pop_front();
        Ok(value)
    }

    /// Actual receipts are counted once; missing transport receipts remain unknown.
    pub fn usage(&self) -> Value {
        let mut totals = BTreeMap::<String, u64>::new();
        let mut costs = vec![];
        let mut known = !self.attempts.is_empty();
        let mut token_unknown = 0usize;
        let mut pending_attempts = 0usize;
        for attempt in self.attempts.values() {
            let usage = &attempt["usage"];
            if attempt["status"] != "completed" {
                pending_attempts += 1;
                known = false;
            }
            if attempt["status"] != "completed"
                || usage["input_tokens"].as_u64().is_none()
                || usage["output_tokens"].as_u64().is_none()
            {
                token_unknown += 1;
            }
            if let Some(values) = usage.as_object() {
                for (key, value) in values {
                    if let Some(value) = value.as_u64() {
                        *totals.entry(key.clone()).or_default() += value;
                    }
                }
            }
            if let Some(cost) = usage["cost_usd"].as_str() {
                costs.push(cost.to_string());
            } else {
                known = false;
            }
        }
        let total = if known { sum_decimals(&costs) } else { None };
        json!({"tokens":totals,"tokens_complete":!self.attempts.is_empty()&&token_unknown==0,
            "tokens_unconfirmed_attempts":token_unknown,"pending_attempts":pending_attempts,
            "cost_known":known&&total.is_some(),"cost_usd":total,"cost_receipts_usd":costs,"attempts":self.attempts.len()})
    }

    /// Only approved candidates can become an answer; stopped candidates stay private.
    pub fn output(&self) -> String {
        if self.status == "completed" {
            self.candidate.clone()
        } else if self.status == "needs_input" {
            texts(&self.goal, "clarifications").join("\n")
        } else {
            String::new()
        }
    }

    pub fn goal_markdown(&self) -> String {
        let mut text = format!(
            "# Goal\n\n{}\n\n",
            self.goal["objective"].as_str().unwrap_or("目的を定義中")
        );
        for (label, key) in [
            ("要件", "requirements"),
            ("抽象語の定義", "quality_terms"),
            ("合否基準", "contract"),
        ] {
            text.push_str(&format!(
                "## {label}\n\n```json\n{}\n```\n\n",
                self.goal[key]
            ));
        }
        text.push_str(&format!(
            "## 未確認事項\n\n{}\n",
            texts(&self.goal, "clarifications").join("\n")
        ));
        text
    }
}

fn sum_decimals(values: &[String]) -> Option<String> {
    let mut total = 0u128;
    for value in values {
        let mut parts = value.split('.');
        let whole = parts.next()?;
        let fraction = parts.next().unwrap_or("");
        if parts.next().is_some()
            || whole.is_empty()
            || fraction.len() > 18
            || !whole
                .bytes()
                .chain(fraction.bytes())
                .all(|byte| byte.is_ascii_digit())
        {
            return None;
        }
        let scale = 10u128.pow(18 - fraction.len() as u32);
        let amount = whole
            .parse::<u128>()
            .ok()?
            .checked_mul(10u128.pow(18))?
            .checked_add(if fraction.is_empty() {
                0
            } else {
                fraction.parse::<u128>().ok()?.checked_mul(scale)?
            })?;
        total = total.checked_add(amount)?;
    }
    let mut result = format!("{}.{:018}", total / 10u128.pow(18), total % 10u128.pow(18));
    while result.ends_with('0') {
        result.pop();
    }
    if result.ends_with('.') {
        result.pop();
    }
    Some(result)
}
