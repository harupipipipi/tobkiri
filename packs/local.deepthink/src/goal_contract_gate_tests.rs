//! Deterministic model/Store fixtures, not a native or real-model acceptance.

use super::*;
use crate::workflow::{AttemptContext, Generated, Ports, Settings};

pub(crate) fn approved_review(context: &Value) -> Value {
    let source = &context["original_goal"];
    let candidate = &context["contract_candidate"];
    let requirements = source["requirements"].as_array().unwrap();
    let criteria = candidate["success_criteria"].as_array().unwrap();
    json!({
        "candidate_digest":context["contract_candidate_digest"],"approved":true,
        "global_checks":GLOBAL_CHECKS.iter().map(|id|json!({"id":id,"passed":true,"evidence":"Fixture assessment of the original request"})).collect::<Vec<_>>(),
        "requirement_checks":requirements.iter().enumerate().map(|(index,requirement)| {
            let ids=criteria.iter().filter(|criterion|criterion["requirement_id"]==requirement["id"]).map(|criterion|criterion["id"].clone()).collect::<Vec<_>>();
            let target=criteria.iter().position(|criterion|criterion["requirement_id"]==requirement["id"]).unwrap();
            json!({"requirement_id":requirement["id"],"criterion_ids":ids,"faithful":true,"source_pointer":format!("/requirements/{index}/description"),"candidate_pointer":format!("/success_criteria/{target}/criterion"),"evidence":"Fixture original requirement comparison"})
        }).collect::<Vec<_>>(),
        "criterion_checks":criteria.iter().enumerate().map(|(index,criterion)| {
            let source=requirements.iter().position(|requirement|requirement["id"]==criterion["requirement_id"]).unwrap();
            json!({"criterion_id":criterion["id"],"satisfiable":true,"measurement_aligned":true,"target_aligned":true,"dependencies_aligned":true,"source_pointer":format!("/requirements/{source}/description"),"candidate_pointer":format!("/success_criteria/{index}/criterion"),"evidence":"Fixture measurable relationship check"})
        }).collect::<Vec<_>>(),"issues":[],"clarifications":[]
    })
}

fn rejected_review(context: &Value, question: bool) -> Value {
    let mut result = approved_review(context);
    result["approved"] = json!(false);
    result["global_checks"][3]["passed"] = json!(false);
    result["criterion_checks"][0]["satisfiable"] = json!(false);
    result["issues"] = json!([{"id":"issue1","category":"measurement",
        "requirement_ids":[context["original_goal"]["requirements"][0]["id"]],
        "criterion_ids":[context["contract_candidate"]["success_criteria"][0]["id"]],
        "source_pointer":"/requirements/0/description","candidate_pointer":"/success_criteria/0/measurement",
        "description":"The measurement compares the intermediate object with the final object.",
        "suggested_change":"Measure the requested final relationship without strengthening the requirement."}]);
    if question {
        result["clarifications"] = json!(["Which original source defines the comparison target?"]);
    }
    result
}

fn config() -> Settings {
    Settings {
        max_review_rounds: 2,
        max_tool_rounds: 3,
        max_contract_retries: 2,
        structured_provider: false,
        tools: vec![],
        skills: vec![],
        profile_checks: vec![],
        environment: json!({"execution_mode":"fixture","approval_policy":"core_gate","output_token_limit":"provider_default"}),
        capability_gaps: vec![],
    }
}

fn new_run() -> Run {
    Run::new(
        "semantic-fixture".into(),
        "fixture-model".into(),
        vec![json!({"role":"user","content":"Explain the requested computation and its check."})],
        config(),
    )
    .unwrap()
}

fn context(messages: &[Value]) -> Value {
    messages
        .iter()
        .rev()
        .filter_map(|message| message["content"].as_str())
        .find_map(|text| {
            text.split_once("\n\ncontext:")
                .map(|(_, value)| serde_json::from_str(value).unwrap())
        })
        .unwrap()
}

#[derive(Default)]
struct FixturePorts {
    calls: Vec<String>,
    claims: BTreeSet<String>,
    saved: Option<Run>,
    contexts: Vec<(String, Value)>,
    reject_reviews: usize,
    question: bool,
    revision_question_once: bool,
    malformed_reviews: usize,
    malformed_revisions: usize,
    malformed_intakes: usize,
    fail_final_review_write: bool,
    write_failed: bool,
    writes_after_failure: usize,
}

impl Ports for FixturePorts {
    fn active(&mut self) -> Result<(), Failure> {
        Ok(())
    }
    fn claim_generation(&mut self, run: &Run, attempt: &AttemptContext) -> Result<(), Failure> {
        run.validate_generation_claim(attempt)?;
        if self.claims.contains(&attempt.generation_key) {
            return Err(invalid("fixture duplicate claim"));
        }
        self.persist(run)?;
        self.claims.insert(attempt.generation_key.clone());
        Ok(())
    }
    fn generate(
        &mut self,
        _model: &str,
        messages: &[Value],
        phase: &str,
        _attempt: &AttemptContext,
    ) -> Result<Generated, Failure> {
        self.calls.push(phase.into());
        let ctx = context(messages);
        self.contexts.push((phase.into(), ctx.clone()));
        let usage = json!({"input_tokens":10,"output_tokens":5,"cost_usd":"0.000001"});
        if (phase.starts_with("goal_contract_review:") && self.malformed_reviews > 0)
            || (phase == "goal_contract_revision:1" && self.malformed_revisions > 0)
            || (phase == "goal_intake" && self.malformed_intakes > 0)
        {
            if phase == "goal_intake" {
                self.malformed_intakes -= 1;
            } else if phase == "goal_contract_revision:1" {
                self.malformed_revisions -= 1;
            } else {
                self.malformed_reviews -= 1;
            }
            return Ok(Generated {
                text: "{".into(),
                usage,
            });
        }
        let result = if phase.starts_with("goal_contract_review:") {
            if self.reject_reviews > 0 {
                self.reject_reviews -= 1;
                rejected_review(&ctx, self.question)
            } else {
                approved_review(&ctx)
            }
        } else if phase == "goal_contract_revision:1" {
            let mut candidate = ctx["contract_candidate"].clone();
            candidate["success_criteria"][0]["measurement"] = json!(
                "Measure only the requested final relationship after the prerequisite result."
            );
            candidate["success_criteria"][0]["target"] = json!(
                "The specified final result matches its verification; the intermediate result is a dependency."
            );
            if self.revision_question_once {
                self.revision_question_once = false;
                candidate["confirmed"] = json!(false);
                candidate["clarifications"] =
                    json!(["Which specified source controls the final measurement?"]);
            }
            json!({"contract":candidate,"changes":["Corrected the measurement relationship without changing requirement identities."]})
        } else {
            let fixtures: Value =
                serde_json::from_str(include_str!("fixtures/phases.json")).unwrap();
            fixtures[phase.split(':').next().unwrap()].clone()
        };
        Ok(Generated{text:json!({"summary":{"objective":"Assess the original Goal","assumptions":[],"decision":"Fixture phase result","next_action":"Retain a durable boundary"},"tool_calls":[],"result":result}).to_string(),usage})
    }
    fn tool(&mut self, _call: &Value) -> Result<Value, Failure> {
        panic!("A semantic phase must never execute tools")
    }
    fn verify_artifact(&mut self, _path: &str) -> Result<Value, Failure> {
        panic!("A semantic phase must never inspect an artifact")
    }
    fn persist(&mut self, run: &Run) -> Result<(), Failure> {
        if self.write_failed {
            self.writes_after_failure += 1;
            return Err(invalid("fixture repeated unknown write"));
        }
        if self.fail_final_review_write
            && run
                .phases
                .last()
                .is_some_and(|phase| phase["phase"] == "goal_contract_review:1")
        {
            self.write_failed = true;
            return Err(Failure {
                code: "core-internal".into(),
                message: "fixture unknown checkpoint commit".into(),
            });
        }
        self.saved = Some(run.clone());
        Ok(())
    }
}

fn next_segment(run: &mut Run, ports: &mut FixturePorts) -> Result<Vec<Value>, Failure> {
    if matches!(run.status.as_str(), "paused" | "awaiting_retry") {
        run.resume_pinned("semantic-fixture", "fixture-model", &config())?;
    }
    run.advance_segment(ports)
}

fn drafted(ports: &mut FixturePorts) -> Run {
    let mut run = new_run();
    for _ in 0..8 {
        if run.phases.len() == 2 {
            break;
        }
        next_segment(&mut run, ports).unwrap();
    }
    assert_eq!(run.phases.len(), 2);
    assert_eq!(run.pending.front().unwrap().id, "goal_contract_review:1");
    run
}

fn legacy(ports: &mut FixturePorts) -> Run {
    let mut run = drafted(ports);
    run.contract_gate = None;
    run.pending = VecDeque::from([Phase {
        id: "preflight".into(),
        extra: json!({}),
    }]);
    // This is an in-memory old-v5 snapshot fixture, not the live private Run.
    run
}

fn adopt(run: &mut Run) {
    let candidate = digest(&run.goal["contract"]);
    run.prepare_goal_contract_review(&candidate, "18", &"f".repeat(64))
        .unwrap();
}

#[test]
fn semantic_gate_blocks_preflight_then_only_a_durably_assessed_revision_is_effective() {
    let mut ports = FixturePorts::default();
    let mut run = drafted(&mut ports);
    assert!(run.effective_goal_contract().is_none());
    assert_eq!(
        run.pause_receipt().unwrap()["next_phase"],
        "goal_contract_review:1"
    );
    let original = run.goal["contract"].clone();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.pending.front().unwrap().id, "preflight");
    assert_eq!(run.effective_goal_contract(), Some(&original));
    assert_eq!(run.contract_gate_status()["state"], "accepted");
    assert_eq!(run.status, "paused");
    assert_eq!(
        ports.calls,
        ["goal_intake", "goal_contract", "goal_contract_review:1"]
    );
    assert_eq!(run.usage()["attempts"], 3);
    assert_eq!(run.usage()["cost_usd"], "0.000003");
}

#[test]
fn explicit_legacy_adoption_preserves_all_old_inputs_records_context_and_costs() {
    let mut ports = FixturePorts {
        malformed_intakes: 2,
        ..Default::default()
    };
    let mut run = legacy(&mut ports);
    assert_eq!(run.attempts.len(), 4);
    let serialized = serde_json::to_value(&run).unwrap();
    assert!(serialized.get("contract_gate").is_none());
    let before = run.clone();
    let blocked = run
        .resume_pinned("semantic-fixture", "fixture-model", &config())
        .unwrap_err();
    assert_eq!(blocked.code, "goal-contract-review-required");
    assert_eq!(serde_json::to_value(&run).unwrap(), serialized);
    adopt(&mut run);
    for key in [
        "id",
        "request_digest",
        "settings",
        "goal",
        "phases",
        "phase_inputs",
        "context",
        "attempts",
        "generations",
        "rejected_generations",
        "generation_resolutions",
        "regeneration_authorizations",
    ] {
        assert_eq!(
            serde_json::to_value(&run).unwrap()[key],
            serialized[key],
            "{key}"
        );
    }
    assert_eq!(run.usage(), before.usage());
    run.advance_segment(&mut ports).unwrap();
    assert_eq!(run.phases[..2], before.phases);
    for (key, value) in before.attempts {
        assert_eq!(run.attempts[&key], value);
    }
    assert_eq!(ports.calls.len(), 5);
    assert_eq!(run.usage()["cost_usd"], "0.000005");
}

#[test]
fn rejected_revision_is_append_only_and_new_context_cannot_be_shadowed_by_old_goal() {
    let mut ports = FixturePorts {
        reject_reviews: 1,
        ..Default::default()
    };
    let mut run = legacy(&mut ports);
    run.context
        .insert("goal".into(), json!({"contract":{"shadow":"old context"}}));
    let before = run.clone();
    adopt(&mut run);
    run.advance_segment(&mut ports).unwrap();
    assert_eq!(run.pending.front().unwrap().id, "goal_contract_revision:1");
    let first = run.contract_gate.as_ref().unwrap().reviews[0].clone();
    let bytes = serde_json::to_vec(&run).unwrap();
    run = serde_json::from_slice(&bytes).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.pending.front().unwrap().id, "goal_contract_review:2");
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.goal["contract"], before.goal["contract"]);
    assert_eq!(run.context, before.context);
    assert_eq!(
        run.phase_inputs["goal_contract@0"],
        before.phase_inputs["goal_contract@0"]
    );
    assert_eq!(
        serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews[0]).unwrap(),
        serde_json::to_value(first).unwrap()
    );
    assert_ne!(run.effective_goal_contract(), Some(&run.goal["contract"]));
    next_segment(&mut run, &mut ports).unwrap();
    let ctx = &ports.contexts.last().unwrap().1;
    assert_eq!(
        ctx["goal"]["contract"],
        *run.effective_goal_contract().unwrap()
    );
    assert_eq!(
        ctx["effective_goal_contract_digest"],
        digest(run.effective_goal_contract().unwrap())
    );
    assert!(run.context["goal"]["contract"].get("shadow").is_some());
}

#[test]
fn second_semantic_rejection_and_restart_cannot_reset_rounds_or_reenter_preflight() {
    let mut ports = FixturePorts {
        reject_reviews: 2,
        ..Default::default()
    };
    let mut run = legacy(&mut ports);
    adopt(&mut run);
    run.advance_segment(&mut ports).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(
        next_segment(&mut run, &mut ports).unwrap_err().code,
        "goal-contract-review-rejected"
    );
    let old = serde_json::to_value(&run).unwrap();
    let mut restored: Run = serde_json::from_value(old.clone()).unwrap();
    assert_eq!(
        restored
            .resume_pinned("semantic-fixture", "fixture-model", &config())
            .unwrap_err()
            .code,
        "goal-contract-gate-exhausted"
    );
    assert!(
        restored
            .prepare_goal_contract_review(
                &digest(&restored.goal["contract"]),
                "18",
                &"f".repeat(64)
            )
            .is_err()
    );
    assert_eq!(serde_json::to_value(&restored).unwrap(), old);
    assert_eq!(restored.contract_gate.as_ref().unwrap().reviews.len(), 2);
    assert!(restored.output().is_empty());
    assert!(!ports.calls.iter().any(|phase| phase == "preflight"));
}

#[test]
fn missing_duplicate_unknown_pointer_and_false_clean_approval_are_rejected() {
    let mut ports = FixturePorts::default();
    let run = drafted(&mut ports);
    let accepted = approved_review(&run.goal_gate_context().unwrap());
    let mut invalid = Vec::new();
    let mut value = accepted.clone();
    value["criterion_checks"] = json!([]);
    invalid.push(value);
    let mut value = accepted.clone();
    let duplicate = value["criterion_checks"][0].clone();
    value["criterion_checks"]
        .as_array_mut()
        .unwrap()
        .push(duplicate);
    invalid.push(value);
    let mut value = accepted.clone();
    value["criterion_checks"][0]["criterion_id"] = json!("invented");
    invalid.push(value);
    let mut value = accepted.clone();
    value["candidate_digest"] = json!("0".repeat(64));
    invalid.push(value);
    let mut value = accepted.clone();
    value["requirement_checks"][0]["source_pointer"] = json!("/objective");
    invalid.push(value);
    let mut value = accepted.clone();
    value["approved"] = json!(false);
    invalid.push(value);
    let mut value = rejected_review(&run.goal_gate_context().unwrap(), false);
    value["approved"] = json!(true);
    invalid.push(value);
    for value in invalid {
        assert!(
            run.validate_goal_gate_result("goal_contract_review:1", &value)
                .is_err()
        );
    }
    assert!(
        run.validate_goal_gate_result("goal_contract_review:1", &accepted)
            .is_ok()
    );
}

#[test]
fn genuine_questions_keep_review_history_and_only_explicit_answer_starts_revision() {
    let mut ports = FixturePorts {
        reject_reviews: 1,
        question: true,
        ..Default::default()
    };
    let mut run = drafted(&mut ports);
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.status, "needs_input");
    let review = run.contract_gate.as_ref().unwrap().reviews[0].clone();
    let calls = ports.calls.len();
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(ports.calls.len(), calls);
    assert!(run.answer("semantic-fixture", " ", &config()).is_err());
    let bytes = serde_json::to_vec(&run).unwrap();
    run = serde_json::from_slice(&bytes).unwrap();
    run.answer(
        "semantic-fixture",
        "Use the final-result comparison in the original source.",
        &config(),
    )
    .unwrap();
    assert_eq!(run.pending.front().unwrap().id, "goal_contract_revision:1");
    assert_eq!(
        serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews[0]).unwrap(),
        serde_json::to_value(review).unwrap()
    );
    assert_eq!(run.answers.len(), 1);
    run.advance_segment(&mut ports).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.contract_gate.as_ref().unwrap().reviews.len(), 2);
    assert_eq!(run.pending.front().unwrap().id, "preflight");
}

#[test]
fn unknown_model_tool_and_checkpoint_outcomes_never_adopt_or_publish_pause() {
    let mut ports = FixturePorts::default();
    let baseline = legacy(&mut ports);
    let mut pending = baseline.clone();
    pending.tool_receipts.insert(
        "unknown".into(),
        super::super::ToolReceipt {
            request_digest: "0".repeat(64),
            status: "pending".into(),
            outcome: None,
        },
    );
    let before = serde_json::to_value(&pending).unwrap();
    assert!(
        pending
            .prepare_goal_contract_review(&digest(&pending.goal["contract"]), "18", &"f".repeat(64))
            .is_err()
    );
    assert_eq!(serde_json::to_value(pending).unwrap(), before);
    let mut model = baseline.clone();
    model.attempts.insert(
        "orphan".into(),
        json!({"phase":"goal_contract","status":"pending","usage":null}),
    );
    assert!(
        model
            .prepare_goal_contract_review(&digest(&model.goal["contract"]), "18", &"f".repeat(64))
            .is_err()
    );
    let mut run = baseline;
    adopt(&mut run);
    ports.fail_final_review_write = true;
    assert_eq!(
        run.advance_segment(&mut ports).unwrap_err().code,
        "checkpoint-unconfirmed"
    );
    assert_ne!(run.status, "paused");
    assert_eq!(ports.writes_after_failure, 0);
    assert!(run.pause_receipt().is_err());
    assert_eq!(
        run.resume_pinned("semantic-fixture", "fixture-model", &config())
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
}

#[test]
fn bounded_json_correction_cannot_be_reset_by_manual_resume() {
    let mut ports = FixturePorts {
        malformed_reviews: 3,
        ..Default::default()
    };
    let mut run = drafted(&mut ports);
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.status, "awaiting_retry");
    assert_eq!(run.retry_receipt().unwrap()["retry"], 1);
    let count = ports.calls.len();
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(ports.calls.len(), count);
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.status, "awaiting_retry");
    assert_eq!(run.retry_receipt().unwrap()["retry"], 2);
    assert!(next_segment(&mut run, &mut ports).is_err());
    assert_eq!(run.status, "quarantined");
    let count = ports.calls.len();
    assert_eq!(
        run.resume_pinned("semantic-fixture", "fixture-model", &config())
            .unwrap_err()
            .code,
        "conflict"
    );
    assert_eq!(ports.calls.len(), count);
    assert_eq!(run.rejected_generations.len(), 3);
    assert_eq!(run.usage()["attempts"], 5);
}

#[test]
fn candidate_digest_revision_encoding_and_existing_gate_are_exact_adoption_bindings() {
    let mut ports = FixturePorts::default();
    let baseline = legacy(&mut ports);
    for (candidate, revision, snapshot) in [
        ("0".repeat(64), "18", "f".repeat(64)),
        (digest(&baseline.goal["contract"]), "018", "f".repeat(64)),
        (
            digest(&baseline.goal["contract"]),
            "18",
            "sha256:wrong".into(),
        ),
    ] {
        let mut run = baseline.clone();
        let before = serde_json::to_value(&run).unwrap();
        assert!(
            run.prepare_goal_contract_review(&candidate, revision, &snapshot)
                .is_err()
        );
        assert_eq!(serde_json::to_value(run).unwrap(), before);
    }
    let mut run = baseline;
    adopt(&mut run);
    let before = serde_json::to_value(&run).unwrap();
    assert!(
        run.prepare_goal_contract_review(&digest(&run.goal["contract"]), "18", &"f".repeat(64))
            .is_err()
    );
    assert_eq!(serde_json::to_value(run).unwrap(), before);
}

#[test]
fn schema_retry_restores_entry_without_rolling_back_a_prior_semantic_review() {
    let mut ports = FixturePorts {
        reject_reviews: 1,
        malformed_revisions: 1,
        ..Default::default()
    };
    let mut run = drafted(&mut ports);
    next_segment(&mut run, &mut ports).unwrap();
    let prior = serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.status, "awaiting_retry");
    assert!(
        run.rejected_generations
            .contains_key("goal_contract_revision:1@0:0#0")
    );
    assert_eq!(
        serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews).unwrap(),
        prior
    );
    let bytes = serde_json::to_vec(&run).unwrap();
    run = serde_json::from_slice(&bytes).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.status, "paused");
    assert_eq!(run.pending.front().unwrap().id, "goal_contract_review:2");
    assert_eq!(
        serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews).unwrap(),
        prior
    );
    assert_eq!(run.usage()["attempts"], 5);
}

#[test]
fn diagnostic_gate_summary_is_compact_and_never_repeats_original_contract_bodies() {
    let mut ports = FixturePorts::default();
    let mut run = drafted(&mut ports);
    next_segment(&mut run, &mut ports).unwrap();
    let summary = run.contract_gate_status();
    assert!(summary.get("original_goal").is_none());
    assert!(summary["candidates"][0].get("contract").is_none());
    assert!(summary["reviews"][0].get("result").is_none());
    assert!(summary.to_string().len() < 4096);
    let mut broken = run;
    broken.contract_gate.as_mut().unwrap().reviews[0].result = json!({});
    assert_eq!(broken.contract_gate_status()["state"], "invalid");
    assert!(broken.effective_goal_contract().is_none());
}

#[test]
fn revision_questions_keep_old_context_and_do_not_erase_or_reset_gate_history() {
    let mut ports = FixturePorts {
        reject_reviews: 1,
        revision_question_once: true,
        ..Default::default()
    };
    let mut run = drafted(&mut ports);
    run.context.insert(
        "clarification_answer".into(),
        json!({"retained":"an earlier real answer"}),
    );
    let original_context = run.context.clone();
    let original_contract = run.goal["contract"].clone();
    next_segment(&mut run, &mut ports).unwrap();
    let first = serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.status, "needs_input");
    assert_eq!(run.context, original_context);
    let bytes = serde_json::to_vec(&run).unwrap();
    run = serde_json::from_slice(&bytes).unwrap();
    run.answer(
        "semantic-fixture",
        "The original request identifies the final measurement.",
        &config(),
    )
    .unwrap();
    assert_eq!(run.context, original_context);
    assert_eq!(run.goal["contract"], original_contract);
    assert_eq!(
        serde_json::to_value(&run.contract_gate.as_ref().unwrap().reviews).unwrap(),
        first
    );
    assert_eq!(run.phase_epochs["goal_contract_revision:1"], 1);
    assert_eq!(run.contract_gate.as_ref().unwrap().answer_records.len(), 1);
    run.advance_segment(&mut ports).unwrap();
    next_segment(&mut run, &mut ports).unwrap();
    assert_eq!(run.contract_gate_status()["state"], "accepted");
    assert_eq!(run.contract_gate.as_ref().unwrap().candidates.len(), 2);
    assert_eq!(run.usage()["attempts"], 6);
}

fn three_item_pointer_fixture() -> (Value, Value, Value) {
    let original = json!({"requirements":(0..3).map(|index| {
        json!({"id":format!("requirement-{index}"),"description":"Original requested relationship"})
    }).collect::<Vec<_>>()});
    let candidate = json!({"success_criteria":(0..3).map(|index| {
        json!({"id":format!("criterion-{index}"),"requirement_id":format!("requirement-{index}"),
            "criterion":"Compare the requested subject","measurement":"Compare the specified relationship",
            "target":"Match the original requirement"})
    }).collect::<Vec<_>>()});
    let context = json!({"original_goal":original,"contract_candidate":candidate,
        "contract_candidate_digest":digest(&candidate)});
    let assessment = approved_review(&context);
    (original, candidate, assessment)
}

#[test]
fn twelve_whole_item_review_pointers_remain_rejected_by_the_domain_contract() {
    let (original, candidate, valid) = three_item_pointer_fixture();
    let candidate_digest = digest(&candidate);
    assert!(validate_review(&original, &candidate, &candidate_digest, &valid).is_ok());
    let result_schema = schema("goal_contract_review:1")["properties"]["result"].clone();
    let mut rejected = 0;
    for collection in ["requirement_checks", "criterion_checks"] {
        for index in 0..3 {
            for (field, array) in [
                ("source_pointer", "requirements"),
                ("candidate_pointer", "success_criteria"),
            ] {
                let mut value = valid.clone();
                value[collection][index][field] = json!(format!("/{array}/{index}"));
                // The provider's JSON schema accepts this string. The domain
                // deliberately requires a real child field, not the item root.
                assert!(phases::validate(&value, &result_schema).is_ok());
                assert!(validate_review(&original, &candidate, &candidate_digest, &value).is_err());
                rejected += 1;
            }
        }
    }
    assert_eq!(rejected, 12);
}

#[test]
fn review_prompt_examples_use_existing_child_fields_and_exclude_whole_objects() {
    let (original, candidate, _) = three_item_pointer_fixture();
    for phase in ["goal_contract_review:1", "goal_contract_review:2"] {
        let instruction = prompt(phase);
        for (source, pointer, prefix) in [
            (&original, "/requirements/0/description", "/requirements/0/"),
            (
                &candidate,
                "/success_criteria/0/measurement",
                "/success_criteria/0/",
            ),
            (
                &candidate,
                "/success_criteria/0/criterion",
                "/success_criteria/0/",
            ),
            (
                &candidate,
                "/success_criteria/0/target",
                "/success_criteria/0/",
            ),
        ] {
            assert!(instruction.contains(pointer));
            assert!(check_pointer(source, &json!(pointer), prefix).is_ok());
        }
        assert!(instruction.contains("whole-object"));
        assert!(instruction.contains("joint_satisfiability"));
        assert!(instruction.contains("公開検証結果"));
        assert!(instruction.contains("私的推論過程を出さない"));
    }
}

#[test]
fn rejected_pointer_feedback_names_the_expected_child_prefix() {
    let (original, candidate, _) = three_item_pointer_fixture();
    for (source, pointer, prefix) in [
        (&original, "/requirements/0", "/requirements/0/"),
        (&candidate, "/success_criteria/0", "/success_criteria/0/"),
    ] {
        let error = check_pointer(source, &json!(pointer), prefix).unwrap_err();
        assert!(error.message.contains(prefix));
        assert!(error.message.contains("child-field"));
    }
}
