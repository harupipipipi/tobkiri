use async_trait::async_trait;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};
use tobkiri_canon::{AllocationBudget, ids::RequestId};
use tobkiri_flow::{RunEvent, RunEventKind, RunJournal, StepError};
use tobkiri_gate::{CallCx, Gate, GrantId, Outcome};

pub(crate) type PendingOutcomes = Arc<Mutex<BTreeMap<RequestId, PendingCompletion>>>;
pub(crate) struct PendingCompletion {
    pub grants: Vec<GrantId>,
    proposal: Option<Outcome>,
}
impl PendingCompletion {
    #[cfg(test)]
    pub fn new(grants: Vec<GrantId>) -> Self {
        Self {
            grants,
            proposal: None,
        }
    }
}

/// Bind the submitted input grant to the terminal state of its actual Flow run.
pub(crate) struct CompletionJournal {
    pub inner: Arc<dyn RunJournal>,
    pub gate: Arc<dyn Gate>,
    pub pending: PendingOutcomes,
}
#[async_trait]
impl RunJournal for CompletionJournal {
    async fn append(
        &self,
        cx: &CallCx,
        event: &RunEvent,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError> {
        let recorded = self.inner.append(cx, event, budget).await;
        let outcome = match &event.kind {
            RunEventKind::Completed { .. } if recorded.is_ok() => Some(Outcome::Succeeded),
            RunEventKind::Completed { .. } => Some(Outcome::Failed {
                code: "internal".into(),
            }),
            RunEventKind::Failed { error } => Some(Outcome::Failed {
                code: error.audit_code().into(),
            }),
            RunEventKind::Cancelled { .. } => Some(Outcome::Cancelled),
            _ => None,
        };
        if let Some(outcome) = outcome {
            let grant = self
                .pending
                .lock()
                .map_err(|_| StepError::new("internal", "run completion map poisoned"))?
                .remove(&event.request);
            if let Some(pending) = grant {
                // The first proposal remains immutable even if the interpreter
                // emits a failure event after this delivery attempt returned an error.
                let outcome = pending.proposal.unwrap_or(outcome);
                let mut retry = Vec::new();
                for grant in pending.grants {
                    if self
                        .gate
                        .record_outcome(grant.clone(), outcome.clone())
                        .await
                        .is_err()
                    {
                        retry.push(grant);
                    }
                }
                if !retry.is_empty() {
                    self.pending
                        .lock()
                        .map_err(|_| StepError::new("internal", "run completion map poisoned"))?
                        .insert(
                            event.request.clone(),
                            PendingCompletion {
                                grants: retry,
                                proposal: Some(outcome),
                            },
                        );
                    return Err(StepError::new(
                        "internal",
                        "run outcome could not be recorded",
                    ));
                }
            }
        }
        recorded
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};
    use tobkiri_canon::{
        Digest,
        ids::{InputSourceId, OpName, ProfileId, ShapeRef},
    };
    use tobkiri_gate::CancellationToken;
    use tobkiri_gate::{
        ApprovalAnswer, ApprovalPrompt, AuditEvent, AuditSink, EffectRequest, GateEngine,
        GateError, MediatorChain, Principal, ResourceLimits, TrustedPrompt,
    };
    use tobkiri_store::{Approvals, DataRoot};

    #[derive(Default)]
    struct Audit(Mutex<Vec<AuditEvent>>, Mutex<BTreeMap<Digest, Vec<u8>>>);
    impl AuditSink for Audit {
        fn append(&self, event: AuditEvent) -> Result<(), GateError> {
            self.0.lock().unwrap().push(event);
            Ok(())
        }
        fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
            crate::test_support::append_once(&self.1, id, &event, || self.append(event.clone()))
        }
    }
    struct Prompt;
    #[async_trait]
    impl TrustedPrompt for Prompt {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            ApprovalAnswer::Deny
        }
    }
    struct Journal;
    #[async_trait]
    impl RunJournal for Journal {
        async fn append(
            &self,
            _: &CallCx,
            _: &RunEvent,
            _: &Arc<dyn AllocationBudget>,
        ) -> Result<(), StepError> {
            Ok(())
        }
    }

    #[tokio::test]
    async fn submitted_input_is_not_a_success_until_its_flow_completes() {
        let dir = tempfile::tempdir().unwrap();
        let root = DataRoot::open(std::fs::canonicalize(dir.path()).unwrap()).unwrap();
        let profile = ProfileId::parse("prof_completion").unwrap();
        let source = InputSourceId::parse("local.input").unwrap();
        let shape = ShapeRef::parse("local.fixture.input@1.0").unwrap();
        let audit = Arc::new(Audit::default());
        let gate = Arc::new(GateEngine::new(
            Arc::new(Approvals::open(&root, &profile).unwrap()),
            audit.clone(),
            Arc::new(Prompt),
            MediatorChain::default(),
        ));
        let limits = ResourceLimits {
            fuel_per_call: 100000,
            memory_mib: 8,
            wall_ms: 1000,
            table_elements: 1024,
            output_bytes: 1024,
        };
        gate.register_input(
            &profile,
            &source,
            [shape.to_string()].into_iter().collect(),
            limits.clone(),
        )
        .unwrap();
        let revision = Digest::hash(b"completion-view");
        gate.register_operation_views(
            &profile,
            &revision,
            BTreeMap::from([(
                shape.clone(),
                [OpName::parse("submit").unwrap()].into_iter().collect(),
            )]),
        )
        .unwrap();
        let cx = CallCx::root(
            profile,
            Principal::Input { source },
            RequestId::parse("req_completion").unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(1),
            limits,
        )
        .with_revision(revision);
        let request = EffectRequest::PortCall {
            shape,
            op: OpName::parse("submit").unwrap(),
        };
        let args = Digest::hash(b"input");
        let permit = gate
            .authorize(&cx, request.clone(), args.clone())
            .await
            .unwrap()
            .consume(&cx, &request, &args)
            .unwrap();
        let pending = Arc::new(Mutex::new(BTreeMap::from([(
            cx.request().clone(),
            PendingCompletion::new(vec![permit.id().clone()]),
        )])));
        let journal = CompletionJournal {
            inner: Arc::new(Journal),
            gate,
            pending: pending.clone(),
        };
        journal
            .append(
                &cx,
                &RunEvent {
                    request: cx.request().clone(),
                    plan_digest: Digest::hash(b"plan"),
                    sequence: 1,
                    kind: RunEventKind::Started,
                },
                &crate::test_support::value_budget(),
            )
            .await
            .unwrap();
        assert!(
            audit
                .0
                .lock()
                .unwrap()
                .iter()
                .all(|event| event.kind != "outcome")
        );
        assert_eq!(pending.lock().unwrap().len(), 1);
        journal
            .append(
                &cx,
                &RunEvent {
                    request: cx.request().clone(),
                    plan_digest: Digest::hash(b"plan"),
                    sequence: 2,
                    kind: RunEventKind::Failed {
                        error: StepError::new("unavailable", "provider failed"),
                    },
                },
                &crate::test_support::value_budget(),
            )
            .await
            .unwrap();
        assert!(pending.lock().unwrap().is_empty());
        let events = audit.0.lock().unwrap();
        let outcome = events.iter().find(|event| event.kind == "outcome").unwrap();
        let bytes =
            tobkiri_canon::encode(&outcome.payload, tobkiri_canon::Variant::StrictV1).unwrap();
        let text = String::from_utf8(bytes).unwrap();
        assert!(text.contains("failed"));
        assert!(!text.contains("succeeded"));
        assert!(!text.contains("provider failed"));
    }

    struct RetryGate {
        calls: Mutex<Vec<(GrantId, Outcome)>>,
        fail_once: std::sync::atomic::AtomicBool,
    }
    #[async_trait]
    impl Gate for RetryGate {
        fn check_context(&self, _: &CallCx) -> Result<(), GateError> {
            Ok(())
        }
        async fn authorize(
            &self,
            _: &CallCx,
            _: EffectRequest,
            _: Digest,
        ) -> Result<tobkiri_gate::Grant, GateError> {
            Err(GateError::Unavailable)
        }
        async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError> {
            self.calls.lock().unwrap().push((grant, outcome));
            if self
                .fail_once
                .swap(false, std::sync::atomic::Ordering::SeqCst)
            {
                Err(GateError::Persistence(
                    "fixture one-shot delivery failure".into(),
                ))
            } else {
                Ok(())
            }
        }
        async fn record_denial(
            &self,
            _: &CallCx,
            _: &'static str,
            _: &'static str,
        ) -> Result<(), GateError> {
            Ok(())
        }
    }
    #[tokio::test]
    async fn completion_attempts_every_grant_and_retry_keeps_initial_terminal_proposal() {
        let gate = Arc::new(RetryGate {
            calls: Mutex::new(Vec::new()),
            fail_once: std::sync::atomic::AtomicBool::new(true),
        });
        // IDs here are bookkeeping for a fake Gate; they confer no authority.
        let ids: Vec<GrantId> =
            serde_json::from_value(serde_json::json!(["grant_first", "grant_second"])).unwrap();
        let cx = CallCx::root(
            ProfileId::parse("prof_journal_retry").unwrap(),
            Principal::User,
            RequestId::parse("req_retry").unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(1),
            ResourceLimits::default(),
        );
        let pending = Arc::new(Mutex::new(BTreeMap::from([(
            cx.request().clone(),
            PendingCompletion::new(ids.clone()),
        )])));
        let journal = CompletionJournal {
            inner: Arc::new(Journal),
            gate: gate.clone(),
            pending: pending.clone(),
        };
        let mut event = RunEvent {
            request: cx.request().clone(),
            plan_digest: Digest::hash(b"plan"),
            sequence: 1,
            kind: RunEventKind::Completed {
                value: crate::test_support::value(&tobkiri_canon::CanonValue::Null),
                cost_units: 1,
            },
        };
        assert!(
            journal
                .append(&cx, &event, &crate::test_support::value_budget())
                .await
                .is_err()
        );
        assert_eq!(gate.calls.lock().unwrap().len(), 2);
        assert_eq!(
            pending.lock().unwrap()[cx.request()].grants,
            vec![ids[0].clone()]
        );
        event.kind = RunEventKind::Failed {
            error: StepError::new("internal", "fixture append error"),
        };
        journal
            .append(&cx, &event, &crate::test_support::value_budget())
            .await
            .unwrap();
        assert!(pending.lock().unwrap().is_empty());
        let calls = gate.calls.lock().unwrap();
        assert_eq!(calls.len(), 3);
        assert_eq!(calls[2].0, ids[0]);
        assert!(
            calls
                .iter()
                .all(|(_, outcome)| matches!(outcome, Outcome::Succeeded))
        );
    }
}
