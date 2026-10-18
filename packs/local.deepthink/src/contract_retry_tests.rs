//! Public segment retries stop before another SQL claim or provider request.
use super::*;

fn waiting_retry() -> (Run, FakePorts, Settings) {
    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        invalid_public_output_once: Some("{".into()),
        ..Default::default()
    };
    let event = run.advance_segment(&mut ports).unwrap();
    assert_eq!(event[0]["status"], "awaiting_retry");
    (run, ports, config)
}

#[test]
fn rejected_public_segment_stops_before_next_claim_and_keeps_zero_phases() {
    let (mut run, mut ports, _) = waiting_retry();
    assert_eq!(run.status, "awaiting_retry");
    assert_eq!(ports.calls, ["goal_intake"]);
    assert_eq!(ports.claims.len(), 1);
    assert_eq!(run.attempts.len(), 1);
    assert!(run.phases.is_empty());
    assert!(run.entry.is_some());
    assert!(run.output().is_empty());
    assert!(run.pause_receipt().is_err());
    assert!(run.requires_explicit_continuation());
    let receipt = run.retry_receipt().unwrap();
    assert_eq!(receipt["completed_phases"], 0);
    assert_eq!(receipt["completed_phases_this_segment"], 0);
    assert_eq!(receipt["phase"], "goal_intake");
    assert_eq!(receipt["next_phase"], "goal_intake");
    assert_eq!(receipt["rejected_attempt"], "goal_intake@0:0#0:0");
    assert_eq!(receipt["prepared_generation"], "goal_intake@0:0#1");
    assert_eq!(receipt["approved"], false);
    assert_eq!(receipt["usage"]["pending_attempts"], 0);
    assert_eq!(run.retry_receipt().unwrap(), receipt);
    let writes = ports.persist_calls;
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(ports.persist_calls, writes);
    assert_eq!(ports.claims.len(), 1);
    assert_eq!(ports.calls.len(), 1);
}

#[test]
fn explicit_continuation_uses_the_prepared_epoch_once_without_recounting() {
    let (run, mut ports, config) = waiting_retry();
    let rejected = serde_json::to_value(&run.rejected_generations).unwrap();
    let old_attempts = run.attempts.clone();
    let inputs = run.phase_inputs.clone();
    let retry_contexts = run.retry_contexts.clone();
    let mut restored: Run =
        serde_json::from_slice(&serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap())
            .unwrap();
    let usage = restored.usage();
    restored
        .resume_pinned("fixture-conversation", "fixture-model", &config)
        .unwrap();
    assert_eq!(restored.status, "running");
    assert_eq!(restored.generation_epochs["goal_intake@0:0"], 1);
    assert_eq!(restored.contract_retries["goal_intake@0:0"], 1);
    assert_eq!(restored.retry_contexts, retry_contexts);
    assert_eq!(restored.usage(), usage);
    assert_eq!(ports.claims.len(), 1); // Resume never creates a permit.
    restored.advance_segment(&mut ports).unwrap();
    assert_eq!(restored.status, "paused");
    assert_eq!(restored.phases.len(), 1);
    assert_eq!(ports.claims.len(), 2);
    assert_eq!(ports.calls, ["goal_intake", "goal_intake"]);
    for (id, attempt) in old_attempts {
        assert_eq!(restored.attempts[&id], attempt);
    }
    assert_eq!(
        serde_json::to_value(&restored.rejected_generations).unwrap(),
        rejected
    );
    assert_eq!(restored.phase_inputs, inputs);
    assert_eq!(restored.usage()["attempts"], 2);
    assert_eq!(restored.usage()["cost_usd"], "0.000002");
}

#[test]
fn explicit_segments_do_not_reset_the_two_correction_bound() {
    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        invalid_public_output_once: Some("{}".into()),
        invalid_public_output_count: 3,
        ..Default::default()
    };
    for number in 1..=2 {
        let event = run.advance_segment(&mut ports).unwrap();
        assert_eq!(event[0]["retry"], number);
        assert_eq!(run.status, "awaiting_retry");
        assert_eq!(ports.calls.len(), number as usize);
        assert_eq!(ports.claims.len(), number as usize);
        run = serde_json::from_slice(&serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap())
            .unwrap();
        run.resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap();
    }
    assert_eq!(
        run.advance_segment(&mut ports).unwrap_err().code,
        "invalid-phase-contract"
    );
    assert_eq!(run.status, "quarantined");
    assert_eq!(run.contract_retries["goal_intake@0:0"], 2);
    assert_eq!(run.rejected_generations.len(), 3);
    assert_eq!(run.usage()["attempts"], 3);
    let before = serde_json::to_value(&run).unwrap();
    assert_eq!(
        run.resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "conflict"
    );
    assert_eq!(serde_json::to_value(&run).unwrap(), before);
    assert!(run.retry_receipt().is_err());
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(ports.calls.len(), 3);
    assert!(run.output().is_empty());
}

#[test]
fn malformed_or_already_claimed_retry_preparation_is_never_resumed() {
    let (run, _, config) = waiting_retry();
    for mutation in 0..8 {
        let mut changed = run.clone();
        match mutation {
            0 => {
                changed.retry_contexts.get_mut("goal_intake@0:0#1").unwrap()["retry_boundary"]["retry"] =
                    json!(2);
            }
            1 => {
                changed
                    .generation_epochs
                    .insert("goal_intake@0:0".into(), 2);
            }
            2 => {
                changed.retry_contexts.get_mut("goal_intake@0:0#1").unwrap()["phase_contract"] =
                    json!({});
            }
            3 => {
                changed.rejected_generations.clear();
            }
            4 => {
                changed.phase_inputs.clear();
            }
            5 => {
                changed.attempts.get_mut("goal_intake@0:0#0:0").unwrap()["context"]["request_digest"] =
                    json!("changed");
            }
            6 => {
                let mut pending = changed.attempts["goal_intake@0:0#0:0"].clone();
                pending["status"] = json!("pending");
                pending["context"]["generation_key"] = json!("goal_intake@0:0#1");
                pending["context"]["attempt_id"] = json!("goal_intake@0:0#1:1");
                pending["usage"] = Value::Null;
                changed
                    .attempts
                    .insert("goal_intake@0:0#1:1".into(), pending);
            }
            _ => {
                changed.goal["objective"] = json!("changed phase input");
            }
        }
        let before = serde_json::to_value(&changed).unwrap();
        assert!(changed.retry_receipt().is_err(), "{mutation}");
        assert!(
            changed
                .resume_pinned("fixture-conversation", "fixture-model", &config)
                .is_err(),
            "{mutation}"
        );
        assert_eq!(serde_json::to_value(&changed).unwrap(), before);
    }
}

#[test]
fn changed_pinned_binding_or_unknown_checkpoint_cannot_consume_retry() {
    let (run, _, config) = waiting_retry();
    for code in ["checkpoint-unconfirmed", "core-internal"] {
        let mut unknown = run.clone();
        unknown.error = Some(Failure {
            code: code.into(),
            message: "unconfirmed operation".into(),
        });
        assert!(unknown.retry_receipt().is_err());
        assert_eq!(
            unknown
                .resume_pinned("fixture-conversation", "fixture-model", &config)
                .unwrap_err()
                .code,
            "needs-reconciliation"
        );
        assert_eq!(unknown.status, "awaiting_retry");
    }
    let mut changed = config.clone();
    changed.environment["binding"] = json!("another-profile-revision");
    assert!(
        run.clone()
            .resume_pinned("fixture-conversation", "fixture-model", &changed)
            .is_err()
    );
    assert!(
        run.clone()
            .resume_pinned("other-conversation", "fixture-model", &config)
            .is_err()
    );
    assert!(
        run.clone()
            .resume_pinned("fixture-conversation", "other-model", &config)
            .is_err()
    );
}

#[test]
fn retry_preparation_commit_unknown_never_publishes_or_retries_a_boundary() {
    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        invalid_public_output_once: Some("{".into()),
        fail_persist_at: Some(5), // entry, phase input, claim, rejection, retry boundary
        ..Default::default()
    };
    let error = run.advance_segment(&mut ports).unwrap_err();
    assert_eq!(error.code, "checkpoint-unconfirmed");
    assert_eq!(run.status, "quarantined");
    assert_eq!(run.error.as_ref().unwrap().code, "checkpoint-unconfirmed");
    assert!(run.retry_receipt().is_err());
    assert_eq!(ports.saved.as_ref().unwrap().status, "quarantined");
    assert_eq!(
        ports
            .saved
            .as_ref()
            .unwrap()
            .attempts
            .values()
            .next()
            .unwrap()["status"],
        "completed"
    );
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(
        run.resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
    assert_eq!(ports.persist_calls, 5);
    assert_eq!(ports.claims.len(), 1);
    assert_eq!(ports.calls.len(), 1);
}

#[test]
fn an_old_rejection_or_unresolved_attempt_is_not_a_new_retry_authority() {
    let (run, ports, config) = waiting_retry();
    let mut old = ports.first_rejection_snapshot.as_ref().unwrap().clone();
    assert_eq!(old.status, "quarantined");
    assert!(old.requires_explicit_continuation());
    assert!(old.retry_receipt().is_err());
    assert_eq!(
        old.resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "conflict"
    );
    // A current pending #2 remains unresolved even if a stale marker exists.
    let mut pending = run.clone();
    let mut attempt = pending.attempts["goal_intake@0:0#0:0"].clone();
    attempt["status"] = json!("pending");
    attempt["context"]["generation_key"] = json!("goal_intake@0:0#2");
    attempt["context"]["attempt_id"] = json!("goal_intake@0:0#2:2");
    attempt["usage"] = Value::Null;
    pending
        .attempts
        .insert("goal_intake@0:0#2:2".into(), attempt);
    assert!(pending.retry_receipt().is_err());
    assert_eq!(
        pending
            .resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
    assert_eq!(pending.status, "awaiting_retry");
}

#[test]
fn unknown_or_same_phase_tool_effects_cannot_become_retry_suspensions() {
    let (run, _, config) = waiting_retry();
    for (key, status, outcome) in [
        ("earlier-phase:tool", "pending", None),
        ("earlier-phase:tool", "completed", None),
        (
            "goal_intake@0:0:tool",
            "completed",
            Some(json!({"ok":true})),
        ),
    ] {
        let mut changed = run.clone();
        changed.tool_receipts.insert(
            key.into(),
            workflow::ToolReceipt {
                request_digest: "0".repeat(64),
                status: status.into(),
                outcome,
            },
        );
        assert!(changed.retry_receipt().is_err());
        assert!(
            changed
                .resume_pinned("fixture-conversation", "fixture-model", &config)
                .is_err()
        );
    }
}

#[test]
fn retry_stream_has_usage_boundary_and_eof_without_done_or_candidate() {
    let (mut run, _, _) = waiting_retry();
    run.candidate = "retained-private-candidate".into();
    // Entry equality is a separate invariant. Keep the retained candidate in it.
    run.entry.as_mut().unwrap().candidate = run.candidate.clone();
    let mut active = crate::Active {
        head: runtime_head(&run),
        run,
        events: Default::default(),
        announced: false,
        terminal_sent: false,
        failure: None,
    };
    let mut events = Vec::new();
    for _ in 0..4 {
        events.extend(active.next(1).unwrap());
    }
    let values = events
        .iter()
        .map(|event| serde_json::from_str::<Value>(event).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        values
            .iter()
            .map(|event| event["kind"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["usage", "progress", "awaiting_retry"]
    );
    assert!(
        events
            .iter()
            .all(|event| !event.contains("retained-private-candidate"))
    );
    assert!(active.next(64).unwrap().is_empty());
}

#[test]
fn retry_boundary_keeps_unknown_accounting_as_unknown_not_zero() {
    let (mut run, _, _) = waiting_retry();
    let usage = json!({"output_tokens":5});
    run.attempts.get_mut("goal_intake@0:0#0:0").unwrap()["usage"] = usage.clone();
    run.rejected_generations
        .get_mut("goal_intake@0:0#0")
        .unwrap()
        .generated
        .usage = usage;
    let receipt = run.retry_receipt().unwrap();
    assert_eq!(receipt["usage"]["tokens_complete"], false);
    assert_eq!(receipt["usage"]["cost_known"], false);
    assert_eq!(receipt["usage"]["cost_usd"], Value::Null);
    assert!(receipt["usage"]["tokens"].get("input_tokens").is_none());
    assert_eq!(receipt["usage"]["pending_attempts"], 0); // Completion known; accounting incomplete.
}
