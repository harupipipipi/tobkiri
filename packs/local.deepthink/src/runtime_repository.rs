//! SQL-authoritative Run cursors and single-use generation dispatch admission.
//! Resources live for one synchronous export/pull, never in the stream registry.

use crate::repository_adapter::CoreDatabase;
use crate::run_repository::{
    Digest, DispatchPermit, GenerationClaim, Head, ProgressRecord, ProgressStamp, RepositoryError,
    Resolution, ResolutionDisposition, RunIdentity, RunRepository, SqlErrorKind,
    SuccessorAuthorization,
};
use crate::tobkiri::core1_1_0::storage;
use crate::workflow::{
    self, AttemptContext, Failure, GenerationProgress, Run, invalid,
    reconciliation::ContentDisposition,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, rc::Rc};

type Repository = RunRepository<CoreDatabase>;

pub struct RunSession {
    repository: Repository,
    head: Head,
    committed: Run,
    progress: BTreeMap<String, (GenerationProgress, ProgressStamp)>,
    dispatch: Option<DispatchPermit>,
    poisoned: bool,
}

pub fn repository_failure(error: RepositoryError) -> Failure {
    let code = match error {
        RepositoryError::CommitUnknown => "checkpoint-unconfirmed",
        RepositoryError::Conflict => "conflict",
        RepositoryError::PendingGeneration => "needs-reconciliation",
        RepositoryError::Limit => "limit-exceeded",
        RepositoryError::Storage(SqlErrorKind::Denied) => "denied",
        RepositoryError::Storage(SqlErrorKind::Cancelled) => "cancelled",
        RepositoryError::Storage(SqlErrorKind::Unavailable) => "unavailable",
        RepositoryError::Storage(SqlErrorKind::Limit) => "limit-exceeded",
        _ => "checkpoint-unconfirmed",
    };
    Failure {
        code: code.into(),
        message: error.to_string(),
    }
}

pub fn claim(context: &AttemptContext) -> Result<GenerationClaim, Failure> {
    Ok(GenerationClaim {
        run_id: context.run_id.clone(),
        run_request_digest: Digest::parse(&context.run_request_digest)
            .map_err(repository_failure)?,
        phase: context.phase.clone(),
        generation_key: context.generation_key.clone(),
        attempt_id: context.attempt_id.clone(),
        request_digest: Digest::parse(&context.request_digest).map_err(repository_failure)?,
        route_digest: Digest::parse(&context.route_digest).map_err(repository_failure)?,
    })
}

fn context(value: &Value) -> Result<AttemptContext, Failure> {
    serde_json::from_value(value["context"].clone())
        .map_err(|_| invalid("model attempt has no exact durable identity"))
}

fn bytes(run: &Run) -> Result<Vec<u8>, Failure> {
    serde_json::to_vec(run).map_err(|_| invalid("workflow checkpoint cannot be serialized"))
}

fn decode_record(
    record: ProgressRecord,
    expected: &AttemptContext,
) -> Result<(GenerationProgress, ProgressStamp), Failure> {
    let progress: GenerationProgress = serde_json::from_slice(&record.bytes)
        .map_err(|_| invalid("SQL generation progress has an invalid public schema"))?;
    if progress.attempt != *expected || progress.sequence != record.stamp.sequence {
        return Err(invalid(
            "SQL generation progress changed its attempt or sequence",
        ));
    }
    Ok((progress, record.stamp))
}

impl RunSession {
    fn repository(store: Rc<storage::Store>) -> Result<Repository, Failure> {
        RunRepository::open(CoreDatabase(store)).map_err(repository_failure)
    }

    /// New read-only operations inspect only existing authoritative snapshots.
    /// No schema initialization, legacy KV fallback, progress merge or dispatch
    /// capability can occur through this loader.
    pub fn load_existing(
        store: Rc<storage::Store>,
        run_id: &str,
    ) -> Result<Option<(Run, Head)>, Failure> {
        let mut repository =
            RunRepository::open_existing(CoreDatabase(store)).map_err(repository_failure)?;
        let Some(snapshot) = repository.load(run_id).map_err(repository_failure)? else {
            return Ok(None);
        };
        let run: Run = serde_json::from_slice(&snapshot.bytes)
            .map_err(|_| invalid("SQL Run snapshot has an invalid schema"))?;
        if run.id != snapshot.head.run_id
            || run.request_digest != snapshot.head.run_request_digest.as_str()
            || run.version != workflow::VERSION
        {
            return Err(invalid("SQL Run snapshot changed its original identity"));
        }
        Ok(Some((run, snapshot.head)))
    }

    pub fn load(store: Rc<storage::Store>, run_id: &str) -> Result<Option<(Run, Head)>, Failure> {
        let mut repository = Self::repository(store)?;
        let Some(snapshot) = repository.load(run_id).map_err(repository_failure)? else {
            return Ok(None);
        };
        let run: Run = serde_json::from_slice(&snapshot.bytes)
            .map_err(|_| invalid("SQL Run snapshot has an invalid schema"))?;
        if run.id != snapshot.head.run_id
            || run.request_digest != snapshot.head.run_request_digest.as_str()
            || run.version != workflow::VERSION
        {
            return Err(invalid("SQL Run snapshot changed its original identity"));
        }
        let mut session = Self {
            repository,
            head: snapshot.head,
            committed: run.clone(),
            progress: BTreeMap::new(),
            dispatch: None,
            poisoned: false,
        };
        let mut run = run;
        for attempt in run.attempts.clone().values() {
            let id = context(attempt)?;
            if attempt["status"] == "completed"
                || run.generation_resolutions.contains_key(&id.attempt_id)
            {
                continue;
            }
            if let Some(progress) = session.read_progress(&id)? {
                run.merge_generation_progress(progress)?;
            }
        }
        Ok(Some((run, session.head)))
    }

    pub fn resume(store: Rc<storage::Store>, expected: &Head) -> Result<Self, Failure> {
        let mut repository = Self::repository(store)?;
        let snapshot = repository
            .load(&expected.run_id)
            .map_err(repository_failure)?
            .ok_or_else(|| invalid("managed workflow was not found"))?;
        if snapshot.head != *expected {
            return Err(repository_failure(RepositoryError::Conflict));
        }
        let run: Run = serde_json::from_slice(&snapshot.bytes)
            .map_err(|_| invalid("SQL Run snapshot has an invalid schema"))?;
        if run.id != expected.run_id || run.request_digest != expected.run_request_digest.as_str() {
            return Err(invalid("managed workflow changed its original identity"));
        }
        Ok(Self {
            repository,
            head: snapshot.head,
            committed: run,
            progress: BTreeMap::new(),
            dispatch: None,
            poisoned: false,
        })
    }

    pub fn create(store: Rc<storage::Store>, run: &Run) -> Result<Self, Failure> {
        if !run.attempts.is_empty()
            || !run.tool_receipts.is_empty()
            || !run.generation_resolutions.is_empty()
            || !run.regeneration_authorizations.is_empty()
        {
            return Err(invalid(
                "historical effects require explicit verified migration",
            ));
        }
        let mut repository = Self::repository(store)?;
        let head = repository
            .create_fresh(
                &RunIdentity {
                    run_id: run.id.clone(),
                    run_request_digest: Digest::parse(&run.request_digest)
                        .map_err(repository_failure)?,
                },
                &bytes(run)?,
            )
            .map_err(repository_failure)?;
        Ok(Self {
            repository,
            head,
            committed: run.clone(),
            progress: BTreeMap::new(),
            dispatch: None,
            poisoned: false,
        })
    }

    pub fn head(&self) -> &Head {
        &self.head
    }

    fn active(&self) -> Result<(), Failure> {
        if self.poisoned {
            return Err(repository_failure(RepositoryError::CommitUnknown));
        }
        Ok(())
    }

    fn failed<T>(&mut self, result: Result<T, RepositoryError>) -> Result<T, Failure> {
        match result {
            Ok(value) => Ok(value),
            Err(error) => {
                self.poisoned = true;
                self.dispatch = None;
                Err(repository_failure(error))
            }
        }
    }

    fn same_identity(&self, run: &Run) -> Result<(), Failure> {
        if run.id != self.head.run_id
            || run.request_digest != self.head.run_request_digest.as_str()
            || run.model != self.committed.model
            || run.conversation != self.committed.conversation
            || workflow::digest(&json!(run.settings))
                != workflow::digest(&json!(self.committed.settings))
        {
            return Err(invalid(
                "managed Run changed its captured request or settings",
            ));
        }
        for (id, old) in &self.committed.attempts {
            let next = run
                .attempts
                .get(id)
                .ok_or_else(|| invalid("a durable attempt was removed"))?;
            if old["context"] != next["context"] {
                return Err(invalid("a durable attempt changed its original binding"));
            }
        }
        for (id, old) in &self.committed.generation_resolutions {
            if run.generation_resolutions.get(id) != Some(old)
                || run.attempts.get(id) != self.committed.attempts.get(id)
            {
                return Err(invalid("frozen reconciliation evidence changed"));
            }
        }
        Ok(())
    }

    pub fn read_progress(
        &mut self,
        attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        self.active()?;
        if let Some((progress, _)) = self.progress.get(&attempt.attempt_id) {
            if progress.attempt != *attempt {
                return Err(invalid("generation progress changed its original attempt"));
            }
            return Ok(Some(progress.clone()));
        }
        let request = claim(attempt)?;
        let record = self.repository.load_progress(&self.head, &request);
        let Some(record) = self.failed(record)? else {
            return Ok(None);
        };
        let (progress, stamp) = decode_record(record, attempt)?;
        let mut verified = self.committed.clone();
        verified.merge_generation_progress(progress.clone())?;
        self.progress
            .insert(attempt.attempt_id.clone(), (progress.clone(), stamp));
        Ok(Some(progress))
    }

    pub fn save_progress(&mut self, progress: &GenerationProgress) -> Result<(), Failure> {
        self.active()?;
        let mut verified = self.committed.clone();
        if let Some(previous) = self.read_progress(&progress.attempt)? {
            verified.merge_generation_progress(previous)?;
        }
        verified.merge_generation_progress(progress.clone())?;
        let payload = serde_json::to_vec(progress)
            .map_err(|_| invalid("generation progress cannot be serialized"))?;
        let request = claim(&progress.attempt)?;
        let saved =
            self.repository
                .save_progress(&self.head, &request, progress.sequence, &payload);
        let stamp = self.failed(saved)?;
        self.progress.insert(
            progress.attempt.attempt_id.clone(),
            (progress.clone(), stamp),
        );
        Ok(())
    }

    fn expected_progress(
        &mut self,
        attempt: &AttemptContext,
        original: &Value,
    ) -> Result<Option<ProgressStamp>, Failure> {
        // Load again in the repository to bind resolution to the actual latest
        // progress. insert_resolution rechecks this same stamp in its CAS TX.
        let request = claim(attempt)?;
        let record = self.repository.load_progress(&self.head, &request);
        let record = self.failed(record)?;
        match record {
            Some(record) => {
                let (progress, stamp) = decode_record(record, attempt)?;
                if original.get("progress") != Some(&json!(progress)) {
                    return Err(repository_failure(RepositoryError::Conflict));
                }
                Ok(Some(stamp))
            }
            None => {
                if original.get("progress").is_some() {
                    return Err(invalid(
                        "managed attempt has no matching authoritative SQL progress",
                    ));
                }
                Ok(None)
            }
        }
    }

    fn settlements(&mut self, run: &Run) -> Result<Vec<Resolution>, Failure> {
        let mut settlements = Vec::new();
        for (id, resolution) in &run.generation_resolutions {
            if self.committed.generation_resolutions.contains_key(id) {
                continue;
            }
            run.validate_resolution(id, resolution)?;
            let expected_progress =
                self.expected_progress(&resolution.attempt, &resolution.original_attempt)?;
            settlements.push(Resolution {
                claim: claim(&resolution.attempt)?,
                expected_progress,
                disposition: match resolution.disposition {
                    ContentDisposition::TerminalContentUnavailable => {
                        ResolutionDisposition::TerminalContentUnavailable
                    }
                    ContentDisposition::RecoveredGenerationPendingValidation => {
                        ResolutionDisposition::AcceptedCompletion
                    }
                },
                receipt: serde_json::to_vec(resolution)
                    .map_err(|_| invalid("resolution cannot be serialized"))?,
            });
        }
        for (id, attempt) in &run.attempts {
            if attempt["status"] != "completed"
                || run.generation_resolutions.contains_key(id)
                || self
                    .committed
                    .attempts
                    .get(id)
                    .is_some_and(|old| old["status"] == "completed")
            {
                continue;
            }
            let context = context(attempt)?;
            let (generation, disposition) = if let Some(generation) =
                run.generations.get(&context.generation_key)
            {
                (generation, ResolutionDisposition::AcceptedCompletion)
            } else if let Some(generation) = run.rejected_generations.get(&context.generation_key) {
                (generation, ResolutionDisposition::RejectedCompletion)
            } else {
                return Err(invalid("completed attempt has no exact generation receipt"));
            };
            if generation.request_digest != context.request_digest {
                return Err(invalid("completed generation changed its original request"));
            }
            let expected_progress = self.expected_progress(&context, attempt)?;
            let receipt = json!({"version":"deepthink.generation-completion-receipt.v1",
                "attempt":context,"usage":attempt["usage"],"generation":generation,
                "phase_acceptance_is_separate":true});
            settlements.push(Resolution {
                claim: claim(&context)?,
                expected_progress,
                disposition,
                receipt: serde_json::to_vec(&receipt)
                    .map_err(|_| invalid("completion receipt cannot be serialized"))?,
            });
        }
        Ok(settlements)
    }

    pub fn persist(&mut self, run: &Run) -> Result<(), Failure> {
        self.active()?;
        self.same_identity(run)?;
        let settlements = self.settlements(run)?;
        let payload = bytes(run)?;
        let updated = self
            .repository
            .commit_snapshot(&self.head, &payload, &settlements);
        self.head = self.failed(updated)?;
        self.committed = run.clone();
        Ok(())
    }

    pub fn claim_generation(&mut self, run: &Run, attempt: &AttemptContext) -> Result<(), Failure> {
        self.active()?;
        self.same_identity(run)?;
        run.validate_generation_claim(attempt)?;
        if self.dispatch.is_some() {
            return Err(repository_failure(RepositoryError::Conflict));
        }
        let request = claim(attempt)?;
        let payload = bytes(run)?;
        let successor = run
            .regeneration_authorizations
            .values()
            .find(|authorization| authorization.new_attempt_id == attempt.attempt_id)
            .cloned();
        let result = if let Some(authorization) = successor {
            run.validate_regeneration(&authorization)?;
            let old = self
                .repository
                .read_resolution(&run.id, &authorization.old_attempt_id);
            let old = self
                .failed(old)?
                .ok_or_else(|| invalid("successor has no committed old resolution"))?;
            let resolution = run
                .generation_resolutions
                .get(&authorization.old_attempt_id)
                .ok_or_else(|| invalid("successor has no captured resolution"))?;
            if resolution.resolution_digest != authorization.resolution_digest
                || old.resolution.receipt
                    != serde_json::to_vec(resolution).map_err(|_| invalid("invalid resolution"))?
            {
                return Err(invalid("successor resolution changed"));
            }
            let receipt = serde_json::to_vec(&authorization)
                .map_err(|_| invalid("invalid regeneration authorization"))?;
            self.repository.claim_successor(
                &self.head,
                &payload,
                &SuccessorAuthorization {
                    old_attempt_id: authorization.old_attempt_id,
                    resolution_digest: old.digest,
                    receipt,
                },
                &request,
            )
        } else {
            self.repository
                .claim_generation(&self.head, &payload, &request)
        };
        let (head, permit) = self.failed(result)?;
        self.head = head;
        self.committed = run.clone();
        self.dispatch = Some(permit);
        Ok(())
    }

    pub fn consume_dispatch(&mut self, attempt: &AttemptContext) -> Result<(), Failure> {
        self.active()?;
        let permit = self
            .dispatch
            .take()
            .ok_or_else(|| repository_failure(RepositoryError::Conflict))?;
        let expected = claim(attempt)?;
        if permit.committed_head() != &self.head || permit.claim() != &expected {
            return Err(repository_failure(RepositoryError::Conflict));
        }
        permit.consume(&expected).map_err(repository_failure)
    }
}
