//! Cleanup for authority that was consumed before dispatch could be started.
// These compatibility primitives retain the original supervision regression.
// Live Session entries use ConsumedEntryTerminal and its retained cleanup task.
#![cfg(test)]

use crate::CoreError;
use tobkiri_gate::{CallCx, DispatchPermit, Gate, GateError, GrantId, Outcome};

/// Attempt every terminal proposal even if an earlier audit delivery fails.
pub(crate) async fn terminalize_all(
    gate: &dyn Gate,
    grants: impl IntoIterator<Item = GrantId>,
    outcome: Outcome,
) -> Result<(), GateError> {
    let mut failure = None;
    for grant in grants {
        if let Err(error) = gate.record_outcome(grant, outcome.clone()).await {
            failure.get_or_insert(error);
        }
    }
    failure.map_or(Ok(()), Err)
}

pub(crate) fn with_cleanup(original: CoreError, cleanup: Result<(), GateError>) -> CoreError {
    match cleanup {
        Ok(()) => original,
        Err(error) => CoreError::Cleanup {
            original: Box::new(original),
            cleanup: Box::new(error.into()),
        },
    }
}

/// No physical dispatch occurred. Supervision failure terminalizes both this
/// consumed grant and all grants previously prepared for the same invocation.
pub(crate) async fn watch_consumed(
    gate: &dyn Gate,
    cx: &CallCx,
    permit: &DispatchPermit,
    previous: &[GrantId],
) -> Result<(), CoreError> {
    if let Err(error) = gate.watch_lease(cx, permit) {
        let cleanup = terminalize_all(
            gate,
            previous
                .iter()
                .cloned()
                .chain(std::iter::once(permit.id().clone())),
            Outcome::Failed {
                code: "lease-supervision".into(),
            },
        )
        .await;
        return Err(with_cleanup(error.into(), cleanup));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use std::{
        collections::BTreeMap,
        sync::{
            Arc, Mutex,
            atomic::{AtomicBool, Ordering},
        },
        time::{Duration, Instant},
    };
    use tobkiri_canon::{
        Digest,
        ids::{InputSourceId, OpName, ProfileId, RequestId, ShapeRef},
    };
    use tobkiri_gate::CancellationToken;
    use tobkiri_gate::{
        ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, AuditSink, EffectRequest,
        GateEngine, MediatorChain, Principal, ResourceLimits, TrustedPrompt,
    };
    use tobkiri_store::{Approvals, DataRoot};
    struct Prompt;
    #[async_trait]
    impl TrustedPrompt for Prompt {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            ApprovalAnswer::Deny
        }
    }
    #[derive(Default)]
    struct Sink {
        fail_once: AtomicBool,
        delivered: Mutex<BTreeMap<Digest, Vec<u8>>>,
    }
    impl AuditSink for Sink {
        fn append(&self, _: AuditEvent) -> Result<(), GateError> {
            Ok(())
        }
        fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
            if self.fail_once.swap(false, Ordering::SeqCst) {
                return Err(GateError::Persistence("fixture one-shot sink error".into()));
            }
            crate::test_support::append_once(&self.delivered, id, &event, || Ok(()))
        }
    }
    #[tokio::test]
    async fn watch_failure_terminalizes_current_and_all_previous_grants_despite_delivery_failure() {
        let dir = tempfile::tempdir().unwrap();
        let root = DataRoot::open(std::fs::canonicalize(dir.path()).unwrap()).unwrap();
        let profile = ProfileId::parse("prof_watch_failure").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let sink = Arc::new(Sink::default());
        let gate = GateEngine::new(
            approvals.clone(),
            sink.clone(),
            Arc::new(Prompt),
            MediatorChain::default(),
        );
        let shape = ShapeRef::parse("local.fixture.input@1.0").unwrap();
        let source = InputSourceId::parse("local.input").unwrap();
        let op = OpName::parse("submit").unwrap();
        let revision = Digest::hash(b"watch-plan");
        let limits = ResourceLimits {
            fuel_per_call: 1000,
            memory_mib: 8,
            wall_ms: 1000,
            table_elements: 64,
            output_bytes: 4096,
        };
        gate.register_input(
            &profile,
            &source,
            [shape.to_string()].into_iter().collect(),
            limits.clone(),
        )
        .unwrap();
        gate.register_operation_views(
            &profile,
            &revision,
            BTreeMap::from([(shape.clone(), [op.clone()].into_iter().collect())]),
        )
        .unwrap();
        let cx = CallCx::root(
            profile,
            Principal::Input { source },
            RequestId::parse("req_watch").unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(1),
            limits,
        )
        .with_revision(revision);
        let request = EffectRequest::PortCall { shape, op };
        let args = Digest::hash(b"args");
        let prior = gate
            .authorize(&cx, request.clone(), args.clone())
            .await
            .unwrap()
            .consume(&cx, &request, &args)
            .unwrap();
        gate.watch_lease(&cx, &prior).unwrap();
        let current = gate
            .authorize(&cx, request.clone(), args.clone())
            .await
            .unwrap()
            .consume(&cx, &request, &args)
            .unwrap();
        cx.cancel().cancel();
        sink.fail_once.store(true, Ordering::SeqCst);
        let error = watch_consumed(&gate, &cx, &current, &[prior.id().clone()])
            .await
            .unwrap_err();
        assert!(
            matches!(error, CoreError::Cleanup { original, cleanup } if matches!(*original, CoreError::Gate(GateError::Cancelled)) && matches!(*cleanup, CoreError::Gate(GateError::Persistence(_))))
        );
        let first = approvals.terminal_delivery(prior.id()).unwrap().unwrap();
        let second = approvals.terminal_delivery(current.id()).unwrap().unwrap();
        for decision in [&first.decision, &second.decision] {
            assert!(
                matches!(&decision.outcome, Outcome::Failed { code } if code == "lease-supervision")
            );
        }
        assert!(!first.acknowledged);
        assert!(second.acknowledged);
        assert_eq!(sink.delivered.lock().unwrap().len(), 1);
        gate.flush_outbox(cx.profile()).await.unwrap();
        assert_eq!(sink.delivered.lock().unwrap().len(), 2);
        assert_eq!(
            approvals
                .terminal_delivery(prior.id())
                .unwrap()
                .unwrap()
                .decision
                .event_id,
            first.decision.event_id
        );
    }
}
