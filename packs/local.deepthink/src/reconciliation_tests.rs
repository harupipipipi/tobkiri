use super::*;

fn run() -> Run {
    Run::new(
        "reconciliation-fixture".into(),
        "fixture/model".into(),
        vec![json!({"role":"user","content":"17×19を2節で検算して"})],
        Settings {
            max_review_rounds: 2,
            max_tool_rounds: 2,
            max_contract_retries: 0,
            structured_provider: false,
            tools: vec![],
            skills: vec![],
            profile_checks: vec![],
            environment: json!({}),
            capability_gaps: vec![],
        },
    )
    .unwrap()
}
fn full() -> String {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/phases.json")).unwrap();
    json!({"summary":{"objective":"明示したGoalを満たす","assumptions":[],"decision":"実要件で検証","next_action":"次工程"},
        "tool_calls":[],"result":fixture["goal_intake"]}).to_string()
}
#[derive(Default)]
struct FixturePorts {
    claims: BTreeMap<String, AttemptContext>,
    permits: BTreeSet<String>,
    progress: BTreeMap<String, GenerationProgress>,
    saved: Option<Run>,
    calls: usize,
    persist_calls: usize,
    fail_transport: bool,
    unknown_commit: bool,
    request_extra_tool: bool,
}
impl Ports for FixturePorts {
    fn active(&mut self) -> Result<(), Failure> {
        Ok(())
    }
    fn claim_generation(&mut self, run: &Run, attempt: &AttemptContext) -> Result<(), Failure> {
        run.validate_generation_claim(attempt)?;
        let key = format!("{}:{}", attempt.run_id, attempt.generation_key);
        if self.claims.contains_key(&key) {
            return Err(Failure {
                code: "conflict".into(),
                message: "fixture existing immutable claim".into(),
            });
        }
        self.claims.insert(key, attempt.clone());
        self.persist(run)?;
        if self.unknown_commit {
            return Err(Failure {
                code: "core-internal".into(),
                message: "fixture commit acknowledgment lost".into(),
            });
        }
        self.permits.insert(attempt.attempt_id.clone());
        Ok(())
    }
    fn generate(
        &mut self,
        model: &str,
        _messages: &[Value],
        _phase: &str,
        attempt: &AttemptContext,
    ) -> Result<Generated, Failure> {
        assert!(
            self.permits.remove(&attempt.attempt_id),
            "a claim must only permit one local dispatch"
        );
        self.calls += 1;
        if self.fail_transport {
            let mut progress = GenerationProgress::new(attempt.clone());
            progress.accept(&json!({"kind":"metadata","text":json!({"version":"tobkiri.chat.metadata.v1","generation_id":"gen-fixture-1","model":model,"provider":"fixture-provider"}).to_string()}))?;
            progress.accept(&json!({"kind":"delta","text":"{"}))?;
            progress.accept(&json!({"kind":"usage","prompt-tokens":2,"completion-tokens":1}))?;
            progress.next_snapshot()?;
            self.progress.insert(attempt.attempt_id.clone(), progress);
            return Err(Failure {
                code: "unavailable".into(),
                message: "fixture public stream stopped".into(),
            });
        }
        let mut output: Value = serde_json::from_str(&full()).unwrap();
        if self.request_extra_tool {
            output["tool_calls"] = json!([{"id":"repeat","name":"artifact.write","arguments":{"path":"x","content":"again"}}]);
            output["result"] = Value::Null;
        }
        Ok(Generated {
            text: output.to_string(),
            usage: json!({"input_tokens":10,"output_tokens":5,"total_tokens":15,"cost_usd":"0.002"}),
        })
    }
    fn generation_progress(
        &mut self,
        attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        Ok(self.progress.get(&attempt.attempt_id).cloned())
    }
    fn tool(&mut self, _: &Value) -> Result<Value, Failure> {
        panic!("readonly reconciliation must not execute a tool")
    }
    fn verify_artifact(&mut self, _: &str) -> Result<Value, Failure> {
        panic!("readonly reconciliation must not verify via a tool")
    }
    fn persist(&mut self, run: &Run) -> Result<(), Failure> {
        self.persist_calls += 1;
        self.saved = Some(run.clone());
        Ok(())
    }
}
fn stopped() -> (Run, FixturePorts, String) {
    let mut run = run();
    let mut ports = FixturePorts {
        fail_transport: true,
        ..Default::default()
    };
    assert_eq!(run.advance(&mut ports).unwrap_err().code, "unavailable");
    let id = run.attempts.keys().next().unwrap().clone();
    (run, ports, id)
}
fn observation(content: Option<String>) -> ReaderObservation {
    serde_json::from_value(json!({"version":OBSERVATION_VERSION,"generation_id":"gen-fixture-1",
        "metadata":{"availability":"available","http_status":200,"model":"fixture/model-20261018","provider":"fixture-provider",
            "identity_diagnostics":{"mismatch_reason":null,"generation_id_matches":true,"model_matches":true,"provider_matches":true,"observed_model":"fixture/model-20261018","observed_provider":"fixture-provider"},
            "reported_latency":null,"reported_generation_time":null,
            "terminal":{"state":"normal_stop_reported","finish_reason":"stop","native_finish_reason":"stop","cancelled":false},
            "observed_usage":{"native_input_tokens":10,"native_output_tokens":5,"native_total_tokens":15,"native_cached_input_tokens":1,"native_reasoning_tokens":1,
                "normalized_input_tokens":12,"normalized_output_tokens":7,"cost_usd":"0.0000000000000000000001","native_tokens_complete":true,"cost_known":true}},
        "content":{"availability":if content.is_some(){"available"}else{"not_found"},"http_status":if content.is_some(){200}else{404},"public_completion":content}
    })).unwrap()
}
fn provenance(observation: &ReaderObservation) -> ObservationProvenance {
    ObservationProvenance {
        version: PROVENANCE_VERSION.into(),
        reader_pack: "local.openrouter-generation-reader".into(),
        reader_contract: READER_PORT.into(),
        reader_pack_hash: None,
        reader_binding_digest: None,
        profile_revision: None,
        requested_model: "fixture/model".into(),
        canonical_model: "fixture/model-20261018".into(),
        observation_digest: digest(&json!(observation)),
        model_mapping: Some(ModelMapping {
            version: "openrouter.model-mapping.v1".into(),
            requested_model: "fixture/model".into(),
            canonical_model: "fixture/model-20261018".into(),
            source_url: "https://openrouter.ai/api/v1/model/fixture/model".into(),
            response_digest: format!("sha256:{}", "a".repeat(64)),
        }),
    }
}
fn resolve(run: &mut Run, id: &str, content: Option<String>) -> Resolution {
    let observation = observation(content);
    let provenance = provenance(&observation);
    run.reconcile_generation(id, observation, provenance)
        .unwrap()
}

#[test]
fn every_new_dispatch_requires_a_confirmed_unique_claim() {
    let mut run = run();
    let mut ports = FixturePorts::default();
    run.advance_segment(&mut ports).unwrap();
    assert_eq!(ports.calls, 1);
    assert_eq!(ports.claims.len(), 1);
    assert!(ports.permits.is_empty());
    assert_eq!(run.status, "paused");
}
#[test]
fn claim_commit_unknown_never_dispatches_or_remints_a_matching_row() {
    let mut run = run();
    let mut ports = FixturePorts {
        unknown_commit: true,
        ..Default::default()
    };
    assert_eq!(
        run.advance(&mut ports).unwrap_err().code,
        "checkpoint-unconfirmed"
    );
    assert_eq!(ports.calls, 0);
    assert!(ports.permits.is_empty());
    assert_eq!(ports.claims.len(), 1);
    let mut restored = ports.saved.clone().unwrap();
    assert_eq!(
        restored.resume(&restored.clone()).unwrap_err().code,
        "needs-reconciliation"
    );
    let attempt = ports.claims.values().next().unwrap().clone();
    assert_eq!(
        ports
            .claim_generation(&restored, &attempt)
            .unwrap_err()
            .code,
        "conflict"
    );
    assert_eq!(ports.calls, 0);
}
#[test]
fn missing_content_freezes_original_attempt_counts_cost_once_and_refuses_normal_continue() {
    let (mut run, ports, id) = stopped();
    let original = run.attempts[&id].clone();
    let settings = digest(&json!(run.settings));
    let original_id = run.id.clone();
    let receipt = resolve(&mut run, &id, None);
    let same = run
        .reconcile_generation(&id, receipt.observation.clone(), receipt.provenance.clone())
        .unwrap();
    assert_eq!(receipt, same);
    assert_eq!(
        receipt.disposition,
        ContentDisposition::TerminalContentUnavailable
    );
    assert_eq!(run.attempts[&id], original);
    assert_eq!(receipt.original_attempt, original);
    assert_eq!(receipt.public_prefix_bytes, 1);
    assert_eq!(run.id, original_id);
    assert_eq!(run.model, "fixture/model");
    assert_eq!(digest(&json!(run.settings)), settings);
    let usage = run.usage();
    assert_eq!(usage["cost_known"], true);
    assert_eq!(usage["tokens_complete"], true);
    assert_eq!(usage["cost_usd"], "0.0000000000000000000001");
    assert_eq!(usage["cost_receipts_usd"].as_array().unwrap().len(), 1);
    assert_eq!(usage["content_incomplete_attempts"], 1);
    assert!(run.output().is_empty());
    assert!(run.generations.is_empty());
    assert_eq!(
        run.resume(&run.clone()).unwrap_err().code,
        "needs-reconciliation"
    );
    assert_eq!(ports.calls, 1);
}
#[test]
fn complete_recovered_public_text_uses_normal_phase_acceptance_without_another_dispatch() {
    let (mut run, mut ports, id) = stopped();
    let original = run.attempts[&id].clone();
    resolve(&mut run, &id, Some(full()));
    assert!(run.phases.is_empty());
    assert!(run.generations.is_empty());
    run.resume(&run.clone()).unwrap();
    run.advance_segment(&mut ports).unwrap();
    assert_eq!(ports.calls, 1);
    assert_eq!(ports.claims.len(), 1);
    assert_eq!(run.status, "paused");
    assert_eq!(run.phases.len(), 1);
    assert!(!run.generations.is_empty());
    assert!(run.output().is_empty());
    assert_eq!(run.attempts[&id], original);
    assert_eq!(
        run.usage()["cost_receipts_usd"].as_array().unwrap().len(),
        1
    );
}
#[test]
fn recovered_malformed_text_is_quarantined_by_the_existing_contract_validator() {
    let (mut run, mut ports, id) = stopped();
    resolve(&mut run, &id, Some("{".into()));
    run.resume(&run.clone()).unwrap();
    assert_eq!(
        run.advance_segment(&mut ports).unwrap_err().code,
        "invalid-phase-contract"
    );
    assert_eq!(run.status, "quarantined");
    assert_eq!(ports.calls, 1);
    assert_eq!(run.rejected_generations.len(), 1);
    assert!(run.phases.is_empty());
    assert!(run.output().is_empty());
}
#[test]
fn mismatched_prefix_is_not_adopted_as_content_or_accounting() {
    let (mut run, _, id) = stopped();
    let observation = observation(Some("different prefix".into()));
    let provenance = provenance(&observation);
    assert!(
        run.reconcile_generation(&id, observation, provenance)
            .is_err()
    );
    assert!(run.generation_resolutions.is_empty());
    assert_eq!(run.usage()["cost_known"], false);
}
#[test]
fn identity_mapping_nonterminal_and_unknown_accounting_cannot_authorize_recovery() {
    for fault in 0..7 {
        let (mut run, _, id) = stopped();
        let mut observation = observation(None);
        let mut proof = provenance(&observation);
        match fault {
            0 => observation.generation_id = "gen-other".into(),
            1 => observation.metadata.provider = Some("wrong".into()),
            2 => observation.metadata.terminal.state = TerminalState::Unknown,
            3 => {
                observation.metadata.observed_usage.cost_usd = None;
                observation.metadata.observed_usage.cost_known = false;
            }
            4 => proof.model_mapping = None,
            5 => {
                proof.model_mapping.as_mut().unwrap().source_url =
                    "https://untrusted.invalid/model".into()
            }
            _ => observation.metadata.observed_usage.native_total_tokens = Some(999),
        }
        proof.observation_digest = digest(&json!(observation));
        assert!(
            run.reconcile_generation(&id, observation, proof).is_err(),
            "fault {fault}"
        );
        assert!(run.generation_resolutions.is_empty());
    }
}
#[test]
fn late_progress_cannot_change_a_resolved_prefix_identity_or_cost() {
    let (mut run, _, id) = stopped();
    resolve(&mut run, &id, None);
    let before = run.attempts[&id].clone();
    let mut late: GenerationProgress = serde_json::from_value(before["progress"].clone()).unwrap();
    late.accept(&json!({"kind":"delta","text":"late"})).unwrap();
    late.next_snapshot().unwrap();
    assert_eq!(
        run.merge_generation_progress(late).unwrap_err().code,
        "conflict"
    );
    assert_eq!(run.attempts[&id], before);
    assert_eq!(run.usage()["cost_usd"], "0.0000000000000000000001");
}
#[test]
fn explicit_regeneration_gets_a_new_epoch_but_ordinary_reload_cannot_start_it() {
    let (mut run, mut ports, id) = stopped();
    let original = run.attempts[&id].clone();
    let resolution = resolve(&mut run, &id, None);
    let authorization = run
        .prepare_regeneration(&id, &resolution.resolution_digest)
        .unwrap();
    assert_ne!(
        authorization.old_generation_key,
        authorization.new_generation_key
    );
    assert!(authorization.new_generation_key.ends_with("#1"));
    let mut restored: Run = serde_json::from_value(json!(run)).unwrap();
    assert!(restored.regeneration_dispatch.is_none());
    assert_eq!(
        restored.resume(&restored.clone()).unwrap_err().code,
        "needs-reconciliation"
    );
    let same = restored
        .prepare_regeneration(&id, &resolution.resolution_digest)
        .unwrap();
    assert_eq!(same, authorization);
    ports.fail_transport = false;
    restored.advance_segment(&mut ports).unwrap();
    assert_eq!(ports.calls, 2);
    assert_eq!(ports.claims.len(), 2);
    assert_eq!(restored.attempts.len(), 2);
    assert_eq!(restored.attempts[&id], original);
    assert_eq!(restored.model, "fixture/model");
    assert_eq!(
        restored.usage()["cost_receipts_usd"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(restored.usage()["cost_usd"], "0.0020000000000000000001");
    assert!(
        restored
            .prepare_regeneration(&id, &resolution.resolution_digest)
            .is_err()
    );
}
#[test]
fn unknown_tool_or_local_checkpoint_custody_is_not_resolved_by_model_metadata() {
    for code in ["checkpoint-unconfirmed", "core-internal", "tool"] {
        let (mut run, _, id) = stopped();
        if code == "tool" {
            run.tool_receipts.insert(
                "unknown".into(),
                ToolReceipt {
                    request_digest: "x".into(),
                    status: "pending".into(),
                    outcome: None,
                },
            );
        } else {
            run.error = Some(Failure {
                code: code.into(),
                message: "unknown custody".into(),
            });
        }
        let observation = observation(None);
        let proof = provenance(&observation);
        assert_eq!(
            run.reconcile_generation(&id, observation, proof)
                .unwrap_err()
                .code,
            "needs-reconciliation"
        );
        assert!(run.generation_resolutions.is_empty());
    }
}
#[test]
fn lossless_large_counters_and_exact_decimal_sums_do_not_overflow_or_round() {
    let (mut run, _, id) = stopped();
    let mut observation = observation(None);
    observation.metadata.observed_usage.native_input_tokens = Some(u64::MAX - 10);
    observation.metadata.observed_usage.native_output_tokens = Some(10);
    observation.metadata.observed_usage.native_total_tokens = Some(u64::MAX);
    let proof = provenance(&observation);
    run.reconcile_generation(&id, observation, proof).unwrap();
    let usage = run.usage();
    assert_eq!(counter(&usage["tokens"]["total_tokens"]), Some(u64::MAX));
    assert!(usage["tokens"]["total_tokens"].is_string());
    run.attempts.insert("other".into(),json!({"status":"completed","usage":{"input_tokens":100,"output_tokens":2,"total_tokens":102,"cost_usd":"0"}}));
    let usage = run.usage();
    assert_eq!(usage["tokens_complete"], false);
    assert!(usage["tokens"].get("input_tokens").is_none());
    assert!(
        usage["tokens_overflow_fields"]
            .as_array()
            .unwrap()
            .contains(&json!("total_tokens"))
    );
    assert_eq!(
        sum_decimals(&["0.0000000000000000000001".into(), "0.002".into()]).as_deref(),
        Some("0.0020000000000000000001")
    );
}
#[test]
fn observation_schema_rejects_private_unknown_fields_floats_and_oversize() {
    let mut value = json!(observation(None));
    value["private_reasoning"] = json!("must not be accepted");
    assert!(decode_observation(&value.to_string()).is_err());
    let mut value = json!(observation(None));
    value["metadata"]["observed_usage"]["native_input_tokens"] = json!(1.5);
    assert!(decode_observation(&value.to_string()).is_err());
    let mut value = json!(observation(None));
    value["metadata"]["observed_usage"]["native_input_tokens"] = json!(u64::MAX.to_string());
    assert_eq!(
        decode_observation(&value.to_string())
            .unwrap()
            .metadata
            .observed_usage
            .native_input_tokens,
        Some(u64::MAX)
    );
    assert!(decode_observation(&"x".repeat(MAX_OBSERVATION_BYTES + 1)).is_err());
}
#[test]
fn changed_immutable_resolution_is_not_a_new_accounting_receipt_or_resume_permission() {
    let (mut run, _, id) = stopped();
    resolve(&mut run, &id, None);
    run.generation_resolutions.get_mut(&id).unwrap().accounting["cost_usd"] = json!("0");
    assert!(run.ensure_generations_reconciled().is_err());
    assert_eq!(run.usage()["cost_known"], false);
    assert_eq!(run.usage()["invalid_resolution_attempts"], 1);
}

#[test]
fn stopped_second_phase_keeps_completed_goal_and_its_separate_accounting_receipt() {
    let mut run = run();
    let mut ports = FixturePorts::default();
    run.advance_segment(&mut ports).unwrap();
    let goal = run.goal.clone();
    let phases = run.phases.clone();
    run.resume(&run.clone()).unwrap();
    ports.fail_transport = true;
    assert_eq!(
        run.advance_segment(&mut ports).unwrap_err().code,
        "unavailable"
    );
    let id = run
        .attempts
        .iter()
        .find(|(_, attempt)| attempt["phase"] == "goal_contract")
        .unwrap()
        .0
        .clone();
    let receipt = resolve(&mut run, &id, None);
    assert_eq!(receipt.attempt.phase, "goal_contract");
    assert_eq!(run.phases, phases);
    assert_eq!(run.goal["requirements"], goal["requirements"]);
    assert_eq!(
        run.usage()["cost_receipts_usd"].as_array().unwrap().len(),
        2
    );
    assert_eq!(ports.calls, 2);
    assert_eq!(
        run.resume(&run.clone()).unwrap_err().code,
        "needs-reconciliation"
    );
}

#[test]
fn explicit_correction_after_completed_effect_refuses_additional_tool_requests() {
    let (mut run, mut ports, id) = stopped();
    let key = "goal_intake@0:0:0";
    run.tool_receipts.insert(
        key.into(),
        ToolReceipt {
            request_digest: "fixture-old-effect".into(),
            status: "completed".into(),
            outcome: Some(json!({"ok":true,"result":{"saved":true}})),
        },
    );
    let original = serde_json::to_value(&run.tool_receipts).unwrap();
    let resolution = resolve(&mut run, &id, None);
    let authorization = run
        .prepare_regeneration(&id, &resolution.resolution_digest)
        .unwrap();
    assert_eq!(
        run.retry_contexts[&authorization.new_generation_key]["allow_additional_tools"],
        false
    );
    ports.fail_transport = false;
    ports.request_extra_tool = true;
    assert_eq!(
        run.advance_segment(&mut ports).unwrap_err().code,
        "conflict"
    );
    assert_eq!(serde_json::to_value(&run.tool_receipts).unwrap(), original);
    assert_eq!(ports.calls, 2);
    assert!(run.output().is_empty());
}
