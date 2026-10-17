//! Explicit observations and discard decisions, never authority to send HTTP.
//! Runtime supplies verified reader observations and commits each immutable row
//! with its Run-head CAS. Only Ports::claim_generation can admit a new dispatch.
use super::*;

pub const OBSERVATION_VERSION: &str = "tobkiri.generation-observation.v1";
pub const PROVENANCE_VERSION: &str = "deepthink.reconciliation-observation.v1";
pub const RESOLUTION_VERSION: &str = "deepthink.generation-resolution.v1";
pub const REGENERATION_VERSION: &str = "deepthink.regeneration-authorization.v1";
pub const READER_PORT: &str = "local.openrouter-generation-reader.generation-reader@1.0";
const MAX_OBSERVATION_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    Available,
    NotRequested,
    MetadataUnavailable,
    NotFound,
    Forbidden,
    Unauthorized,
    RateLimited,
    HttpFailure,
    InvalidResponse,
    ResponseTooLarge,
    IdentityMismatch,
    IdentityUnconfirmed,
    NoStoredOutput,
    ProviderError,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FinishReason {
    Stop,
    Length,
    ContentFilter,
    ToolCalls,
    Error,
    Cancelled,
    Other,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TerminalState {
    Unknown,
    CancellationReported,
    NormalStopReported,
    IncompleteFinishReported,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum IdentityMismatchReason {
    GenerationId,
    Model,
    Provider,
    Multiple,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct IdentityDiagnostics {
    pub mismatch_reason: Option<IdentityMismatchReason>,
    pub generation_id_matches: bool,
    pub model_matches: bool,
    pub provider_matches: Option<bool>,
    pub observed_model: Option<String>,
    pub observed_provider: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TerminalObservation {
    pub state: TerminalState,
    pub finish_reason: Option<FinishReason>,
    pub native_finish_reason: Option<FinishReason>,
    pub cancelled: Option<bool>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct UsageObservation {
    #[serde(default, deserialize_with = "optional_counter")]
    pub native_input_tokens: Option<u64>,
    #[serde(default, deserialize_with = "optional_counter")]
    pub native_output_tokens: Option<u64>,
    #[serde(default, deserialize_with = "optional_counter")]
    pub native_total_tokens: Option<u64>,
    #[serde(default, deserialize_with = "optional_counter")]
    pub native_cached_input_tokens: Option<u64>,
    #[serde(default, deserialize_with = "optional_counter")]
    pub native_reasoning_tokens: Option<u64>,
    #[serde(default, deserialize_with = "optional_counter")]
    pub normalized_input_tokens: Option<u64>,
    #[serde(default, deserialize_with = "optional_counter")]
    pub normalized_output_tokens: Option<u64>,
    pub cost_usd: Option<String>,
    pub native_tokens_complete: bool,
    pub cost_known: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MetadataObservation {
    pub availability: Availability,
    pub http_status: u16,
    pub model: Option<String>,
    pub provider: Option<String>,
    pub identity_diagnostics: Option<IdentityDiagnostics>,
    pub reported_latency: Option<String>,
    pub reported_generation_time: Option<String>,
    pub terminal: TerminalObservation,
    pub observed_usage: UsageObservation,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ContentObservation {
    pub availability: Availability,
    pub http_status: Option<u16>,
    pub public_completion: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ReaderObservation {
    pub version: String,
    pub generation_id: String,
    pub metadata: MetadataObservation,
    pub content: ContentObservation,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ModelMapping {
    pub version: String,
    pub requested_model: String,
    pub canonical_model: String,
    pub source_url: String,
    pub response_digest: String,
}
/// Supplied by the trusted Runtime reader call, never by a User accounting JSON.
/// Core audit separately retains actual binding/epoch. Missing introspection is
/// recorded as None, not fabricated from old Settings or a hardcoded current pin.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ObservationProvenance {
    pub version: String,
    pub reader_pack: String,
    pub reader_contract: String,
    pub reader_pack_hash: Option<String>,
    pub reader_binding_digest: Option<String>,
    pub profile_revision: Option<String>,
    pub requested_model: String,
    pub canonical_model: String,
    pub observation_digest: String,
    pub model_mapping: Option<ModelMapping>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ContentDisposition {
    TerminalContentUnavailable,
    RecoveredGenerationPendingValidation,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Resolution {
    pub version: String,
    pub attempt: AttemptContext,
    pub original_attempt: Value,
    pub observation: ReaderObservation,
    pub provenance: ObservationProvenance,
    pub disposition: ContentDisposition,
    pub public_prefix_sha256: String,
    pub public_prefix_bytes: u64,
    pub progress_sequence: u64,
    pub accounting: Value,
    pub resolution_digest: String,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RegenerationAuthorization {
    pub version: String,
    pub old_attempt_id: String,
    pub resolution_digest: String,
    pub old_generation_key: String,
    pub new_generation_key: String,
    pub new_attempt_id: String,
    pub route_digest: String,
    pub authorization_digest: String,
}

fn needs(message: &str) -> Failure {
    Failure {
        code: "needs-reconciliation".into(),
        message: message.into(),
    }
}
fn bounded_identity(value: &str) -> bool {
    !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
}
fn hash(value: &str) -> bool {
    let value = value.strip_prefix("sha256:").unwrap_or(value);
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}
pub(super) fn counter(value: &Value) -> Option<u64> {
    value.as_u64().or_else(|| {
        let text = value.as_str()?;
        if text.is_empty()
            || text.len() > 20
            || (text.len() > 1 && text.starts_with('0'))
            || !text.bytes().all(|byte| byte.is_ascii_digit())
        {
            return None;
        }
        text.parse().ok()
    })
}
fn optional_counter<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<u64>, D::Error> {
    let value = Option::<Value>::deserialize(deserializer)?;
    value
        .map(|value| {
            counter(&value).ok_or_else(|| serde::de::Error::custom("invalid lossless counter"))
        })
        .transpose()
}
pub(super) fn counter_value(value: u64) -> Value {
    if value > 9_007_199_254_740_991 {
        json!(value.to_string())
    } else {
        json!(value)
    }
}
pub(super) fn decimal_valid(value: &str) -> bool {
    if value.is_empty() || value.len() > 256 {
        return false;
    }
    let mut parts = value.split('.');
    let whole = parts.next().unwrap();
    let fraction = parts.next();
    !whole.is_empty()
        && (whole.len() == 1 || !whole.starts_with('0'))
        && whole.bytes().all(|byte| byte.is_ascii_digit())
        && fraction
            .is_none_or(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
        && parts.next().is_none()
}
pub fn decode_observation(text: &str) -> Result<ReaderObservation, Failure> {
    if text.len() > MAX_OBSERVATION_BYTES {
        return Err(invalid("reader observation exceeds its bound"));
    }
    serde_json::from_str(text)
        .map_err(|_| invalid("reader observation has an invalid closed schema"))
}

impl Resolution {
    fn digest(&self) -> String {
        let mut value = json!(self);
        value["resolution_digest"] = json!("");
        digest(&value)
    }
    pub fn generated(&self) -> Option<Generated> {
        if self.disposition != ContentDisposition::RecoveredGenerationPendingValidation {
            return None;
        }
        self.observation
            .content
            .public_completion
            .as_ref()
            .map(|text| Generated {
                text: text.clone(),
                usage: self.accounting.clone(),
            })
    }
}
impl RegenerationAuthorization {
    fn digest(&self) -> String {
        let mut value = json!(self);
        value["authorization_digest"] = json!("");
        digest(&value)
    }
}

impl Run {
    fn reconciliation_ready(&self) -> Result<(), Failure> {
        if self.version != VERSION
            || self.id != self.request_digest
            || self.error.as_ref().is_some_and(|error| {
                matches!(
                    error.code.as_str(),
                    "checkpoint-unconfirmed" | "core-internal"
                )
            })
            || self.tool_receipts.values().any(|receipt| {
                receipt.status != "completed"
                    || receipt
                        .outcome
                        .as_ref()
                        .is_none_or(|outcome| outcome["ok"] != true)
            })
        {
            return Err(needs(
                "Uncertain checkpoint, Core or tool custody must be reconciled separately; a model observation cannot authorize replay.",
            ));
        }
        Ok(())
    }
    fn exact_attempt(&self, id: &str) -> Result<(AttemptContext, GenerationProgress), Failure> {
        let original = self
            .attempts
            .get(id)
            .ok_or_else(|| invalid("original model attempt was not found"))?;
        let context: AttemptContext = serde_json::from_value(original["context"].clone())
            .map_err(|_| needs("Legacy attempt lacks its exact request and route binding."))?;
        let progress: GenerationProgress = serde_json::from_value(original["progress"].clone())
            .map_err(|_| {
                needs("Original public progress and provider generation id are required.")
            })?;
        progress.validate_snapshot()?;
        if context.attempt_id != id
            || context.run_id != self.id
            || context.run_request_digest != self.request_digest
            || context.route_digest != digest(&json!({"model":self.model,"settings":self.settings}))
            || progress.attempt != context
            || original["phase"] != context.phase
        {
            return Err(invalid(
                "original attempt changed its exact workflow or route",
            ));
        }
        Ok((context, progress))
    }

    /// Pure preparation only. Runtime must atomically append this resolution and
    /// its new snapshot under the current SQL head before returning it to User.
    pub fn reconcile_generation(
        &mut self,
        attempt_id: &str,
        observation: ReaderObservation,
        provenance: ObservationProvenance,
    ) -> Result<Resolution, Failure> {
        self.reconciliation_ready()?;
        if let Some(previous) = self.generation_resolutions.get(attempt_id) {
            self.validate_resolution(attempt_id, previous)?;
            if previous.observation == observation && previous.provenance == provenance {
                return Ok(previous.clone());
            }
            return Err(Failure {
                code: "conflict".into(),
                message: "An immutable generation resolution already owns this attempt.".into(),
            });
        }
        let (context, progress) = self.exact_attempt(attempt_id)?;
        if self.generations.contains_key(&context.generation_key)
            || self
                .rejected_generations
                .contains_key(&context.generation_key)
        {
            return Err(invalid(
                "a finalized generation does not need external reconciliation",
            ));
        }
        let accounting = validate_observation(self, &progress, &observation, &provenance)?;
        let complete = observation.metadata.terminal.state == TerminalState::NormalStopReported
            && observation.content.availability == Availability::Available
            && observation.content.http_status == Some(200)
            && observation.content.public_completion.is_some();
        if let Some(text) = &observation.content.public_completion {
            if !text.starts_with(&progress.public_text) {
                return Err(invalid(
                    "recovered public completion differs from its saved prefix",
                ));
            }
        }
        let mut resolution = Resolution {
            version: RESOLUTION_VERSION.into(),
            attempt: context,
            original_attempt: self.attempts[attempt_id].clone(),
            observation,
            provenance,
            disposition: if complete {
                ContentDisposition::RecoveredGenerationPendingValidation
            } else {
                ContentDisposition::TerminalContentUnavailable
            },
            public_prefix_sha256: format!("{:x}", Sha256::digest(progress.public_text.as_bytes())),
            public_prefix_bytes: progress.public_text.len() as u64,
            progress_sequence: progress.sequence,
            accounting,
            resolution_digest: String::new(),
        };
        resolution.resolution_digest = resolution.digest();
        self.generation_resolutions
            .insert(attempt_id.into(), resolution.clone());
        self.status = "quarantined".into();
        self.error = Some(needs(if complete {
            "Recovered public completion awaits the ordinary phase contract and review path; no phase or tool was completed by lookup."
        } else {
            "Provider accounting is terminal but complete public content is unavailable. The original prefix is frozen. Explicit discard-and-regenerate is required for a new request."
        }));
        Ok(resolution)
    }

    pub fn validate_resolution(&self, id: &str, resolution: &Resolution) -> Result<(), Failure> {
        let (context, progress) = self.exact_attempt(id)?;
        if resolution.version != RESOLUTION_VERSION
            || resolution.attempt != context
            || self.attempts.get(id) != Some(&resolution.original_attempt)
            || resolution.resolution_digest != resolution.digest()
            || resolution.public_prefix_bytes != progress.public_text.len() as u64
            || resolution.progress_sequence != progress.sequence
            || resolution.public_prefix_sha256
                != format!("{:x}", Sha256::digest(progress.public_text.as_bytes()))
            || resolution.accounting
                != validate_observation(
                    self,
                    &progress,
                    &resolution.observation,
                    &resolution.provenance,
                )?
        {
            return Err(invalid("immutable generation resolution binding changed"));
        }
        let recovered = resolution.observation.metadata.terminal.state
            == TerminalState::NormalStopReported
            && resolution.observation.content.availability == Availability::Available
            && resolution.observation.content.http_status == Some(200)
            && resolution.observation.content.public_completion.is_some();
        if recovered
            != (resolution.disposition == ContentDisposition::RecoveredGenerationPendingValidation)
            || resolution
                .observation
                .content
                .public_completion
                .as_ref()
                .is_some_and(|text| !text.starts_with(&progress.public_text))
        {
            return Err(invalid("resolution content disposition or prefix changed"));
        }
        Ok(())
    }

    pub(super) fn recovered_generation(
        &self,
        key: &str,
    ) -> Result<Option<CachedGeneration>, Failure> {
        for (id, resolution) in &self.generation_resolutions {
            if resolution.attempt.generation_key == key {
                self.validate_resolution(id, resolution)?;
                return Ok(resolution.generated().map(|generated| CachedGeneration {
                    request_digest: resolution.attempt.request_digest.clone(),
                    generated,
                }));
            }
        }
        Ok(None)
    }

    pub(super) fn reconciled_attempt(&self, id: &str, explicit: bool) -> Result<bool, Failure> {
        let Some(resolution) = self.generation_resolutions.get(id) else {
            return Ok(false);
        };
        self.validate_resolution(id, resolution)?;
        if resolution.disposition == ContentDisposition::RecoveredGenerationPendingValidation {
            return Ok(true);
        }
        let Some(authorization) = self.regeneration_authorizations.get(id) else {
            return Ok(false);
        };
        self.validate_regeneration(authorization)?;
        if self.attempts.contains_key(&authorization.new_attempt_id) {
            return Ok(true);
        }
        // A deserialized Run never has this marker. An ordinary continuation
        // cannot turn an unclaimed explicit preparation into a model request.
        Ok(explicit && self.regeneration_dispatch.as_deref() == Some(id))
    }

    pub fn validate_regeneration(
        &self,
        authorization: &RegenerationAuthorization,
    ) -> Result<(), Failure> {
        let resolution = self
            .generation_resolutions
            .get(&authorization.old_attempt_id)
            .ok_or_else(|| invalid("regeneration has no immutable resolution"))?;
        self.validate_resolution(&authorization.old_attempt_id, resolution)?;
        if authorization.version != REGENERATION_VERSION
            || authorization.authorization_digest != authorization.digest()
            || authorization.resolution_digest != resolution.resolution_digest
            || resolution.disposition != ContentDisposition::TerminalContentUnavailable
            || authorization.old_generation_key != resolution.attempt.generation_key
            || authorization.route_digest != resolution.attempt.route_digest
        {
            return Err(invalid(
                "regeneration authorization changed its frozen resolution",
            ));
        }
        let (base, epoch) = authorization
            .old_generation_key
            .rsplit_once('#')
            .ok_or_else(|| invalid("old generation epoch is invalid"))?;
        let next = epoch
            .parse::<u32>()
            .ok()
            .and_then(|epoch| epoch.checked_add(1))
            .ok_or_else(|| invalid("generation epoch is invalid"))?;
        if authorization.new_generation_key != format!("{base}#{next}")
            || !authorization
                .new_attempt_id
                .strip_prefix(&format!("{}:", authorization.new_generation_key))
                .is_some_and(|number| {
                    !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit())
                })
        {
            return Err(invalid(
                "successor must have a distinct next epoch and attempt identity",
            ));
        }
        if let Some(attempt) = self.attempts.get(&authorization.new_attempt_id) {
            let context: AttemptContext = serde_json::from_value(attempt["context"].clone())
                .map_err(|_| invalid("successor attempt binding is invalid"))?;
            if context.attempt_id != authorization.new_attempt_id
                || context.generation_key != authorization.new_generation_key
                || context.phase != resolution.attempt.phase
                || context.run_id != self.id
                || context.run_request_digest != self.request_digest
                || context.route_digest != authorization.route_digest
            {
                return Err(invalid("successor claim changed its authorization"));
            }
        }
        Ok(())
    }

    /// Structural validation shared with the repository adapter. This supplies
    /// no durable permit; only a successful unique insertion and commit can.
    pub fn validate_generation_claim(&self, attempt: &AttemptContext) -> Result<(), Failure> {
        self.reconciliation_ready()?;
        if self.status != "running"
            || attempt.run_id != self.id
            || attempt.run_request_digest != self.request_digest
            || attempt.route_digest != digest(&json!({"model":self.model,"settings":self.settings}))
            || !hash(&attempt.request_digest)
            || self.attempts.get(&attempt.attempt_id).is_none_or(|saved| {
                saved["context"] != json!(attempt) || saved["status"] != "pending"
            })
            || self
                .last_generation
                .as_ref()
                .is_none_or(|point| point.key != attempt.generation_key)
            || self
                .pending
                .front()
                .is_none_or(|phase| phase.id != attempt.phase)
            || self.generations.contains_key(&attempt.generation_key)
            || self
                .rejected_generations
                .contains_key(&attempt.generation_key)
        {
            return Err(invalid(
                "generation claim does not match its exact pending dispatch",
            ));
        }
        if let Some(old) = &self.regeneration_dispatch {
            let authorization = self
                .regeneration_authorizations
                .get(old)
                .ok_or_else(|| invalid("explicit generation preparation has no authorization"))?;
            self.validate_regeneration(authorization)?;
            if attempt.attempt_id != authorization.new_attempt_id
                || attempt.generation_key != authorization.new_generation_key
            {
                return Err(invalid(
                    "dispatch differs from explicit successor preparation",
                ));
            }
        }
        for (id, previous) in &self.attempts {
            if id == &attempt.attempt_id || self.reconciled_attempt(id, false)? {
                continue;
            }
            let key = previous
                .pointer("/context/generation_key")
                .and_then(Value::as_str)
                .unwrap_or("");
            if previous["status"] != "completed"
                || (!self.generations.contains_key(key)
                    && !self.rejected_generations.contains_key(key))
            {
                return Err(needs(
                    "A previous generation still requires reconciliation before another dispatch.",
                ));
            }
        }
        Ok(())
    }

    /// A User explicitly discards missing content. This does not grant provider
    /// authority: the exact pending successor still needs a winning SQL claim.
    pub fn prepare_regeneration(
        &mut self,
        old_attempt_id: &str,
        resolution_digest: &str,
    ) -> Result<RegenerationAuthorization, Failure> {
        self.reconciliation_ready()?;
        let resolution = self
            .generation_resolutions
            .get(old_attempt_id)
            .cloned()
            .ok_or_else(|| needs("An immutable terminal accounting resolution is required."))?;
        self.validate_resolution(old_attempt_id, &resolution)?;
        if resolution.resolution_digest != resolution_digest
            || resolution.disposition != ContentDisposition::TerminalContentUnavailable
        {
            return Err(invalid(
                "explicit discard does not match an unavailable-content resolution",
            ));
        }
        for id in self.attempts.keys() {
            if id != old_attempt_id && !self.reconciled_attempt(id, false)? {
                let attempt = &self.attempts[id];
                if attempt["status"] != "completed" {
                    return Err(needs(
                        "Another pending model request must be reconciled first.",
                    ));
                }
                let key = attempt
                    .pointer("/context/generation_key")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if !self.generations.contains_key(key)
                    && !self.rejected_generations.contains_key(key)
                {
                    return Err(needs(
                        "Another model completion has no retained generation.",
                    ));
                }
            }
        }
        let point = self
            .last_generation
            .as_ref()
            .ok_or_else(|| invalid("failed generation has no phase position"))?;
        if point.key != resolution.attempt.generation_key || self.entry.is_none() {
            return Err(invalid("only the retained failed phase can be regenerated"));
        }
        let base_key = point.base_key.clone();
        let authorization = if let Some(previous) =
            self.regeneration_authorizations.get(old_attempt_id)
        {
            self.validate_regeneration(previous)?;
            if self.attempts.contains_key(&previous.new_attempt_id) {
                return Err(needs(
                    "The successor was already claimed; inspect that attempt instead of sending it again.",
                ));
            }
            previous.clone()
        } else {
            let next = self
                .generation_epochs
                .get(&base_key)
                .copied()
                .unwrap_or(0)
                .checked_add(1)
                .ok_or_else(|| invalid("generation epochs are exhausted"))?;
            let key = format!("{base_key}#{next}");
            let mut authorization = RegenerationAuthorization {
                version: REGENERATION_VERSION.into(),
                old_attempt_id: old_attempt_id.into(),
                resolution_digest: resolution_digest.into(),
                old_generation_key: resolution.attempt.generation_key.clone(),
                new_attempt_id: format!("{key}:{}", self.attempts.len()),
                new_generation_key: key.clone(),
                route_digest: resolution.attempt.route_digest.clone(),
                authorization_digest: String::new(),
            };
            authorization.authorization_digest = authorization.digest();
            self.generation_epochs.insert(base_key.clone(), next);
            self.regeneration_authorizations
                .insert(old_attempt_id.into(), authorization.clone());
            let phase_prefix = format!(
                "{}:",
                base_key
                    .rsplit_once(':')
                    .ok_or_else(|| invalid("invalid generation position"))?
                    .0
            );
            let additional = !self
                .tool_receipts
                .keys()
                .any(|key| key.starts_with(&phase_prefix));
            self.retry_contexts.insert(key,json!({"discarded_generation":resolution.attempt.generation_key,
                "resolution_digest":resolution_digest,"completed_tool_receipts":self.tool_receipts,
                "phase_contract":phases::schema(&resolution.attempt.phase),
                "phase_execution_policy":phases::execution_policy(&resolution.attempt.phase),
                "allow_additional_tools":additional,"instruction":"User explicitly discarded unavailable model content. Keep the original Goal and completed tool receipts. Never repeat their effects."}));
            authorization
        };
        let entry = self.entry.as_ref().unwrap().clone();
        self.pending = entry.pending;
        self.goal = entry.goal;
        self.context = entry.context;
        self.sections = entry.sections;
        self.candidate = entry.candidate;
        self.candidate_state = entry.candidate_state;
        self.status = "running".into();
        self.error = None;
        self.regeneration_dispatch = Some(old_attempt_id.into());
        Ok(authorization)
    }
}

fn validate_observation(
    run: &Run,
    progress: &GenerationProgress,
    observation: &ReaderObservation,
    provenance: &ObservationProvenance,
) -> Result<Value, Failure> {
    if !bounded_identity(&run.model)
        || observation
            .content
            .public_completion
            .as_ref()
            .is_some_and(|text| text.len() > MAX_OBSERVATION_BYTES)
        || observation
            .metadata
            .identity_diagnostics
            .as_ref()
            .is_some_and(|diagnostic| {
                [&diagnostic.observed_model, &diagnostic.observed_provider]
                    .into_iter()
                    .flatten()
                    .any(|value| !bounded_identity(value))
            })
        || [
            &observation.metadata.reported_latency,
            &observation.metadata.reported_generation_time,
        ]
        .into_iter()
        .flatten()
        .any(|value| !decimal_valid(value))
        || serde_json::to_vec(observation)
            .map_err(|_| invalid("invalid observation"))?
            .len()
            > MAX_OBSERVATION_BYTES
    {
        return Err(invalid("reader observation exceeds its bound"));
    }
    let metadata = &observation.metadata;
    if observation.version != OBSERVATION_VERSION
        || metadata.availability != Availability::Available
        || metadata.http_status != 200
        || observation.generation_id.len() > 128
        || !observation.generation_id.starts_with("gen-")
        || !observation
            .generation_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        || progress.metadata["generation_id"] != observation.generation_id
        || metadata.model.as_deref() != Some(provenance.canonical_model.as_str())
        || !bounded_identity(&provenance.canonical_model)
        || provenance.requested_model != run.model
        || provenance.version != PROVENANCE_VERSION
        || provenance.reader_pack != "local.openrouter-generation-reader"
        || provenance.reader_contract != READER_PORT
        || provenance.observation_digest != digest(&json!(observation))
        || [
            &provenance.reader_pack_hash,
            &provenance.reader_binding_digest,
            &provenance.profile_revision,
        ]
        .into_iter()
        .flatten()
        .any(|value| !hash(value))
        || metadata
            .identity_diagnostics
            .as_ref()
            .is_some_and(|identity| {
                identity.mismatch_reason.is_some()
                    || !identity.generation_id_matches
                    || !identity.model_matches
                    || identity.provider_matches == Some(false)
                    || identity
                        .observed_model
                        .as_ref()
                        .is_some_and(|value| metadata.model.as_ref() != Some(value))
                    || identity
                        .observed_provider
                        .as_ref()
                        .is_some_and(|value| metadata.provider.as_ref() != Some(value))
            })
        || metadata
            .provider
            .as_ref()
            .is_some_and(|provider| !bounded_identity(provider))
        || progress.metadata.get("provider").is_some_and(|old| {
            metadata
                .provider
                .as_ref()
                .map(|value| json!(value))
                .as_ref()
                != Some(old)
        })
    {
        return Err(needs(
            "Reader identity, original route or observation provenance is not confirmed.",
        ));
    }
    let same = provenance.requested_model == provenance.canonical_model;
    if let Some(mapping) = &provenance.model_mapping {
        if mapping.version != "openrouter.model-mapping.v1"
            || mapping.requested_model != run.model
            || mapping.canonical_model != provenance.canonical_model
            || !mapping.response_digest.starts_with("sha256:")
            || !hash(&mapping.response_digest)
            || !run
                .model
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"/_-.".contains(&byte))
            || mapping.source_url != format!("https://openrouter.ai/api/v1/model/{}", run.model)
        {
            return Err(needs(
                "Requested-model catalog mapping was not verified by the reader.",
            ));
        }
    } else if !same {
        return Err(needs(
            "An exact reader-verified canonical model mapping is required.",
        ));
    }
    if progress
        .metadata
        .get("model")
        .and_then(Value::as_str)
        .is_some_and(|model| model != run.model && model != provenance.canonical_model)
    {
        return Err(invalid(
            "saved public model identity differs from the verified mapping",
        ));
    }
    let terminal = &metadata.terminal;
    let terminal_known = match terminal.state {
        TerminalState::NormalStopReported => {
            terminal.finish_reason == Some(FinishReason::Stop) && terminal.cancelled == Some(false)
        }
        TerminalState::CancellationReported => terminal.cancelled == Some(true),
        TerminalState::IncompleteFinishReported => {
            terminal.finish_reason.as_ref().is_some_and(|reason| {
                matches!(
                    reason,
                    FinishReason::Length
                        | FinishReason::ContentFilter
                        | FinishReason::ToolCalls
                        | FinishReason::Error
                        | FinishReason::Cancelled
                )
            })
        }
        TerminalState::Unknown => false,
    };
    let usage = &metadata.observed_usage;
    let (Some(input), Some(output), Some(total), Some(cost)) = (
        usage.native_input_tokens,
        usage.native_output_tokens,
        usage.native_total_tokens,
        usage.cost_usd.as_ref(),
    ) else {
        return Err(needs(
            "Final native accounting remains unknown; it was not inferred as zero.",
        ));
    };
    if !terminal_known
        || !usage.native_tokens_complete
        || !usage.cost_known
        || !decimal_valid(cost)
        || input.checked_add(output) != Some(total)
        || usage
            .native_cached_input_tokens
            .is_some_and(|cached| cached > input)
        || usage
            .native_reasoning_tokens
            .is_some_and(|reasoning| reasoning > output)
    {
        return Err(needs(
            "Provider terminal state and exact final accounting are not both confirmed.",
        ));
    }
    let mut accounting = json!({"input_tokens":counter_value(input),"output_tokens":counter_value(output),"total_tokens":counter_value(total),"cost_usd":cost});
    for (key, value) in [
        ("cached_input_tokens", usage.native_cached_input_tokens),
        ("reasoning_tokens", usage.native_reasoning_tokens),
    ] {
        if let Some(value) = value {
            accounting[key] = counter_value(value);
        }
    }
    for key in [
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    ] {
        if let Some(old) = progress.usage.get(key).and_then(counter) {
            if accounting
                .get(key)
                .and_then(counter)
                .is_none_or(|value| value < old)
            {
                return Err(invalid(
                    "final accounting regressed from saved public evidence",
                ));
            }
        }
    }
    if progress
        .usage
        .get("cost_usd")
        .is_some_and(|old| old != &accounting["cost_usd"])
    {
        return Err(invalid("final cost differs from an existing exact receipt"));
    }
    if observation
        .content
        .public_completion
        .as_ref()
        .is_some_and(|text| text.len() > MAX_OBSERVATION_BYTES)
        || (observation.content.availability == Availability::Available
            && (observation.content.http_status != Some(200)
                || observation.content.public_completion.is_none()))
        || (observation.content.availability != Availability::Available
            && observation.content.public_completion.is_some())
    {
        return Err(invalid(
            "reader content disposition has an invalid closed schema",
        ));
    }
    Ok(accounting)
}

#[cfg(test)]
#[path = "reconciliation_tests.rs"]
mod tests;
