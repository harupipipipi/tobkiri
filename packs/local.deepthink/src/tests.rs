use crate::{
    phases,
    workflow::{self, Failure, Generated, Ports, Run, Settings, Tool},
};
use serde_json::{Value, json};
use std::collections::BTreeSet;

fn settings() -> Settings {
    Settings {
        max_review_rounds: 2,
        max_tool_rounds: 3,
        tools: vec![],
        skills: vec![],
        profile_checks: vec![],
        environment: json!({"execution_mode":"verified_wasm","approval_policy":"core_gate","output_token_limit":"provider_default"}),
        capability_gaps: vec![
            "physical filesystem, shell and 3D editing are not implemented by this Profile".into(),
        ],
    }
}
fn new_run(config: Settings) -> Run {
    Run::new(
        "fixture-conversation".into(),
        "fixture-model".into(),
        vec![json!({"role":"user","content":"17×19を説明して"})],
        config,
    )
    .unwrap()
}
fn fixture(phase: &str) -> Value {
    let all: Value = serde_json::from_str(include_str!("fixtures/phases.json")).unwrap();
    all[phase.split(':').next().unwrap()].clone()
}
fn summary() -> Value {
    json!({"objective":"正確に説明","assumptions":[],"decision":"測定基準で検証","next_action":"次の工程を実施"})
}
fn context(messages: &[Value]) -> Value {
    messages
        .iter()
        .rev()
        .filter_map(|message| message["content"].as_str())
        .find_map(|text| {
            text.split_once("\n\ncontext:")
                .map(|(_, context)| serde_json::from_str(context).unwrap())
        })
        .unwrap()
}

#[derive(Default)]
struct FakePorts {
    calls: Vec<String>,
    effects: u32,
    saved: Option<Run>,
    contexts: Vec<(String, Value)>,
    terms: bool,
    ambiguous: bool,
    reject: bool,
    tool_mode: bool,
    fail_after_tool: bool,
    failed_once: bool,
    deny_tool: bool,
    cancelled: bool,
    pause_preflight: bool,
    tool_in_profile_check: bool,
    bad_selection_once: bool,
    missing_criterion_once: bool,
    two_requirements: bool,
    bad_generation_seen: bool,
    partial_tool_error: bool,
}
impl Ports for FakePorts {
    fn active(&mut self) -> Result<(), Failure> {
        if self.cancelled {
            Err(Failure {
                code: "cancelled".into(),
                message: "fixture cancellation".into(),
            })
        } else {
            Ok(())
        }
    }
    fn generate(
        &mut self,
        _model: &str,
        messages: &[Value],
        phase: &str,
    ) -> Result<Generated, Failure> {
        self.calls.push(phase.into());
        let ctx = context(messages);
        self.contexts.push((phase.into(), ctx.clone()));
        let mut result = fixture(phase);
        let mut calls = json!([]);
        if phase == "goal_intake" && self.two_requirements {
            result["requirements"].as_array_mut().unwrap().push(json!({"id":"r2","description":"検証式を示す","acceptance_criteria":["検証式がある"]}));
        }
        if phase == "goal_contract" && self.two_requirements {
            if self.missing_criterion_once && !self.bad_generation_seen {
                self.bad_generation_seen = true;
            } else {
                result["success_criteria"].as_array_mut().unwrap().push(json!({"id":"c2","requirement_id":"r2","criterion":"検証式がある","measurement":"式を確認","target":"1本以上"}));
            }
        }
        if (phase == "integration" || phase.starts_with("review:")) && self.two_requirements {
            result["acceptance_results"].as_array_mut().unwrap().push(json!({"criterion_id":"c2","criterion":"検証式がある","passed":true,"evidence":"17×(20−1)=323"}));
        }
        if phase == "means_selection" && self.bad_selection_once && !self.bad_generation_seen {
            self.bad_generation_seen = true;
            result["selected_tools"] = json!(["not-provided"]);
        }
        if phase.starts_with("profile_check:") && self.tool_in_profile_check {
            calls = json!([{"id":"not-permitted","name":"artifact.write","arguments":{"path":"unexpected.md","content":"x"}}]);
            result = Value::Null;
        }
        if phase == "preflight"
            && self.pause_preflight
            && ctx["user_answers"].as_array().unwrap().is_empty()
        {
            result["clarifications"] = json!(["用途を指定してください。"]);
        }
        if phase == "goal_intake" && self.terms {
            result["quality_terms"] = json!([{"term":"分かりやすい","context":"説明"},{"term":"完全","context":"元要求"}]);
        }
        if phase.starts_with("quality_terms:") {
            result["term"] = ctx["current_quality_term"]["term"].clone();
            if self.ambiguous {
                result["resolved"] = json!(false);
                result["definition"] = json!("");
                result["metrics"] = json!([]);
                result["clarifications"] = json!(["対象読者を指定してください。"]);
            }
        }
        if phase == "means_selection" && self.tool_mode {
            result["selected_tools"] = json!(["artifact.write"]);
        }
        if phase.starts_with("section:") {
            result["section_id"] = ctx["current_section"]["id"].clone();
            if self.tool_mode && phase == "section:calculation" {
                if !messages.last().unwrap()["content"]
                    .as_str()
                    .unwrap()
                    .starts_with("Verified real tool results:")
                {
                    calls = json!([{"id":"write-one","name":"artifact.write","arguments":{"path":"answer.md","content":"323"}}]);
                    if self.partial_tool_error {
                        calls
                            .as_array_mut()
                            .unwrap()
                            .push(json!({"id":"unknown","name":"not-selected","arguments":{}}));
                    }
                    result = Value::Null;
                } else if self.fail_after_tool && !self.failed_once {
                    self.failed_once = true;
                    return Err(Failure {
                        code: "unavailable".into(),
                        message: "fixture transport failure after effect".into(),
                    });
                } else {
                    result["artifact_paths"] = json!(["answer.md"]);
                }
            }
        }
        if phase.starts_with("review:") && self.reject {
            result["approved"] = json!(false);
            result["blocking_issues"] = json!(["証拠不足"]);
            result["acceptance_results"][0]["passed"] = json!(false);
        }
        let envelope = json!({"summary":summary(),"tool_calls":calls,"result":result});
        phases::validate(&envelope, &phases::schema(phase)).unwrap();
        Ok(Generated {
            text: envelope.to_string(),
            usage: json!({"input_tokens":10,"output_tokens":5,"cost_usd":"0.000001"}),
        })
    }
    fn tool(&mut self, _call: &Value) -> Result<Value, Failure> {
        self.effects += 1;
        if self.deny_tool {
            Ok(json!({"ok":false,"error":{"code":"denied","message":"fixture"}}))
        } else {
            Ok(json!({"ok":true,"result":{"path":"answer.md","size":3,"digest":"sha256:fixture"}}))
        }
    }
    fn verify_artifact(&mut self, path: &str) -> Result<Value, Failure> {
        if self.effects == 0 {
            return Err(workflow::invalid("missing actual artifact"));
        }
        Ok(json!({"path":path,"kind":"tool-owned-storage-artifact","digest":"sha256:fixture"}))
    }
    fn persist(&mut self, run: &Run) -> Result<(), Failure> {
        self.saved = Some(run.clone());
        Ok(())
    }
}
fn complete(run: &mut Run, ports: &mut FakePorts) {
    for _ in 0..40 {
        if run.status != "running" {
            return;
        }
        run.advance(ports).unwrap();
    }
    panic!("workflow did not finish within the explicit fixture schedule");
}
fn tool_settings() -> Settings {
    let mut config = settings();
    config.tools = vec![Tool {
        name: "artifact.write".into(),
        description: "Write a real logical artifact".into(),
        parameters: json!({"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"],"additionalProperties":false}),
    }];
    config
}

#[test]
fn phases_are_sequential_and_only_independently_approved_output_is_published() {
    let mut run = new_run(settings());
    let mut ports = FakePorts::default();
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "completed");
    assert!(run.output().contains("323"));
    assert_eq!(
        &ports.calls[ports.calls.len() - 4..],
        [
            "section:calculation",
            "section:verification",
            "integration",
            "review:1"
        ]
    );
    assert_eq!(run.review_rounds, 1);
    assert_eq!(
        run.usage()["tokens"]["input_tokens"],
        ports.calls.len() * 10
    );
    assert_eq!(run.usage()["cost_known"], true);
    assert_eq!(run.usage()["tokens_complete"], true);
    let text = serde_json::to_string(&ports.contexts).unwrap();
    assert!(!text.contains("max_tokens"));
    assert!(!text.contains("max_output_tokens"));
    let second = &ports
        .contexts
        .iter()
        .find(|(phase, _)| phase == "section:verification")
        .unwrap()
        .1;
    assert_eq!(second["completed_sections"][0]["section_id"], "calculation");
}

#[test]
fn abstract_terms_are_defined_one_at_a_time_and_ambiguous_goal_is_preserved() {
    let mut run = new_run(settings());
    let mut ports = FakePorts {
        terms: true,
        ambiguous: true,
        ..Default::default()
    };
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "needs_input");
    assert_eq!(ports.calls, ["goal_intake", "quality_terms:1"]);
    assert_eq!(
        ports.saved.as_ref().unwrap().goal["quality_terms"][0]["definition_result"]["resolved"],
        false
    );
    assert!(run.goal_markdown().contains("分かりやすい"));
    assert!(run.output().contains("対象読者"));
    let mut run = new_run(settings());
    let mut ports = FakePorts {
        terms: true,
        ..Default::default()
    };
    complete(&mut run, &mut ports);
    assert_eq!(
        &ports.calls[..4],
        [
            "goal_intake",
            "quality_terms:1",
            "quality_terms:2",
            "goal_contract"
        ]
    );
}

#[test]
fn final_review_failure_quarantines_candidates_and_preserves_all_receipts() {
    let mut run = new_run(settings());
    let mut ports = FakePorts {
        reject: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert_eq!(run.error.as_ref().unwrap().code, "review-rejected");
    assert_eq!(run.review_rounds, 2);
    assert_eq!(run.output(), "");
    assert!(!run.candidate.is_empty());
    assert!(ports.calls.contains(&"revision:1".into()));
    assert!(ports.saved.as_ref().unwrap().attempts.len() > 10);
}

#[test]
fn review_requires_exact_unique_full_goal_criterion_coverage() {
    let expected = BTreeSet::from(["c1".into(), "c2".into()]);
    let candidate = json!({"open_issues":[]});
    let mut review = fixture("review");
    assert!(!workflow::review_approved(&review, &candidate, &expected));
    review["acceptance_results"] = json!([{"criterion_id":"c1","passed":true,"evidence":"checked"},{"criterion_id":"c2","passed":true,"evidence":"checked"}]);
    assert!(workflow::review_approved(&review, &candidate, &expected));
    review["acceptance_results"][1]["criterion_id"] = json!("c1");
    assert!(!workflow::review_approved(&review, &candidate, &expected));
    review["acceptance_results"][1]["criterion_id"] = json!("unknown");
    assert!(!workflow::review_approved(&review, &candidate, &expected));
    review["approved"] = json!("true");
    assert!(!workflow::review_approved(&review, &candidate, &expected));
}

#[test]
fn transport_resume_reuses_generation_and_tool_receipts_without_repeating_effect() {
    let config = tool_settings();
    let expected = new_run(config.clone());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        tool_mode: true,
        fail_after_tool: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert_eq!(ports.effects, 1);
    let completed_calls = ports
        .calls
        .iter()
        .filter(|phase| phase.as_str() == "goal_intake")
        .count();
    let saved = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
    let mut restored: Run = serde_json::from_slice(&saved).unwrap();
    restored.resume(&expected).unwrap();
    complete(&mut restored, &mut ports);
    assert_eq!(restored.status, "completed");
    assert_eq!(ports.effects, 1);
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "goal_intake")
            .count(),
        completed_calls
    );
    assert_eq!(restored.usage()["cost_known"], false); // one transport attempt has no receipt
    assert_eq!(restored.usage()["tokens_complete"], false);
    assert_eq!(restored.usage()["pending_attempts"], 1);
    assert_eq!(restored.usage()["tokens_unconfirmed_attempts"], 1);
    assert_eq!(
        restored.usage()["tokens"]["input_tokens"],
        (ports.calls.len() - 1) * 10
    );
    assert!(restored.artifacts.contains_key("answer.md"));
}

#[test]
fn unknown_tool_outcomes_stop_for_reconciliation_and_changed_model_cannot_resume() {
    let expected = new_run(tool_settings());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        tool_mode: true,
        deny_tool: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    let before = ports.calls.len();
    assert!(run.resume(&expected).is_err());
    assert_eq!(ports.calls.len(), before);
    assert_eq!(ports.effects, 1);
    let mut changed = expected.clone();
    changed.request_digest = "different-model-or-configuration".into();
    assert!(run.resume(&changed).is_err());
}

#[test]
fn cancellation_before_generation_preserves_checkpoint_and_makes_no_model_call() {
    let mut run = new_run(settings());
    let expected = run.clone();
    let mut ports = FakePorts {
        cancelled: true,
        ..Default::default()
    };
    assert!(run.advance(&mut ports).is_err());
    assert!(ports.calls.is_empty());
    assert!(ports.saved.is_some());
    ports.cancelled = false;
    run.resume(&expected).unwrap();
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "completed");
}

#[test]
fn inventories_are_omitted_before_assessment_and_filtered_after_selection() {
    let mut run = new_run(tool_settings());
    let mut ports = FakePorts {
        tool_mode: true,
        ..Default::default()
    };
    complete(&mut run, &mut ports);
    for (phase, context) in &ports.contexts {
        if matches!(
            phase.as_str(),
            "goal_intake" | "goal_contract" | "preflight" | "answer_plan"
        ) {
            assert!(context.get("tool_inventory").is_none());
        }
        if phase == "capability_assessment" {
            assert_eq!(context["tool_inventory"][0]["name"], "artifact.write");
        }
        if phase == "work_environment" {
            assert_eq!(context["tool_inventory"][0]["name"], "artifact.write");
        }
    }
}

#[test]
fn workflow_bounds_are_explicit_and_phase_contracts_reject_invalid_json_types() {
    for limit in [0, 6] {
        let mut config = settings();
        config.max_review_rounds = limit;
        assert!(config.validate().is_err());
    }
    let mut config = settings();
    config.max_review_rounds = 5;
    assert!(config.validate().is_ok());
    let envelope = json!({"summary":summary(),"tool_calls":[],"result":{"approved":"true","feedback":"ok","blocking_issues":[],"acceptance_results":[]}});
    assert!(phases::validate(&envelope, &phases::schema("review:1")).is_err());
}

#[test]
fn generic_port_receives_a_json_object_before_the_typed_guest_codec() {
    let call = json!({"id":"receipt-1","name":"calculator","arguments":{"operation":"multiply","operands":[17,19]}});
    let arguments = crate::tool_port_arguments(&call);
    assert!(arguments["call"].is_object());
    assert_eq!(arguments["call"]["name"], "calculator");
    assert_eq!(
        serde_json::from_str::<Value>(&arguments["call"].to_string()).unwrap(),
        call
    );
}

#[test]
fn answering_a_paused_phase_preserves_goal_earlier_phases_tokens_and_old_cache() {
    let config = settings();
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        pause_preflight: true,
        ..Default::default()
    };
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "needs_input");
    let contract = run.goal["contract"].clone();
    let phases = run.phases.clone();
    let calls = ports.calls.len();
    let old_cache = run.generations.keys().cloned().collect::<Vec<_>>();
    let before_usage = run.usage()["tokens"]["input_tokens"].as_u64().unwrap();
    let saved = serde_json::to_vec(&run).unwrap();
    let mut run: Run = serde_json::from_slice(&saved).unwrap();
    run.answer("fixture-conversation", "学校の授業用です。", &config)
        .unwrap();
    assert_eq!(run.goal["contract"], contract);
    assert_eq!(run.phases, phases);
    assert_eq!(run.usage()["tokens"]["input_tokens"], before_usage);
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "completed");
    assert_eq!(ports.calls[calls], "preflight");
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "goal_intake")
            .count(),
        1
    );
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "goal_contract")
            .count(),
        1
    );
    assert!(
        old_cache
            .iter()
            .all(|key| run.generations.contains_key(key))
    );
    assert!(run.generations.contains_key("preflight@1:0#0"));
    assert_eq!(
        run.phases
            .iter()
            .filter(|phase| phase["phase"] == "preflight")
            .count(),
        2
    );
}

#[test]
fn continuation_pins_settings_and_uses_original_snapshot_after_conversation_history_changes() {
    let config = settings();
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        cancelled: true,
        ..Default::default()
    };
    let _ = run.advance(&mut ports);
    let original_messages = run.messages.clone();
    run.resume_pinned("fixture-conversation", "fixture-model", &config)
        .unwrap();
    assert_eq!(run.messages, original_messages);
    let mut changed = config.clone();
    changed.environment["provider_revision"] = json!("another-route");
    assert!(
        run.resume_pinned("fixture-conversation", "fixture-model", &changed)
            .is_err()
    );
    assert!(
        run.resume_pinned("other-conversation", "fixture-model", &config)
            .is_err()
    );
    assert!(
        run.resume_pinned("fixture-conversation", "other-model", &config)
            .is_err()
    );
}

#[test]
fn duplicate_active_registration_refuses_before_any_storage_write() {
    let run = new_run(settings());
    crate::STATE.with_borrow_mut(|state| {
        *state = crate::State::default();
        state.streams.insert(
            1,
            crate::Active {
                run: run.clone(),
                events: std::collections::VecDeque::new(),
                announced: false,
                terminal_sent: true,
                failure: None,
            },
        );
    });
    // Native pure tests have no Core storage implementation: reaching persist
    // would fail. The active guard must return before any Core import executes.
    let error = crate::register(run).unwrap_err();
    assert!(matches!(error.code, crate::ErrorCode::Conflict));
    crate::STATE.with_borrow_mut(|state| *state = crate::State::default());
}

#[test]
fn declarative_profile_checks_cannot_request_tool_execution() {
    let mut config = tool_settings();
    config.profile_checks.push(workflow::ProfileCheck {
        id: "local-check".into(),
        label: "宣言型確認".into(),
        instructions: "成果と照合".into(),
        required: true,
    });
    let mut run = new_run(config);
    let mut ports = FakePorts {
        tool_mode: true,
        tool_in_profile_check: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert_eq!(ports.effects, 0);
    assert!(
        run.error
            .as_ref()
            .unwrap()
            .message
            .contains("declarative phase")
    );
}

#[test]
fn invalid_selected_tool_retries_only_the_bad_generation_and_preserves_prior_work() {
    let expected = new_run(settings());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        bad_selection_once: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert_eq!(ports.effects, 0);
    assert!(run.generations.contains_key("means_selection@0:0#0"));
    let prior = run.phases.clone();
    let usage = run.usage();
    run.resume(&expected).unwrap();
    assert_eq!(run.phases, prior);
    assert_eq!(run.usage(), usage);
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "completed");
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "goal_intake")
            .count(),
        1
    );
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "means_selection")
            .count(),
        2
    );
    assert!(run.generations.contains_key("means_selection@0:0#0"));
    assert!(run.generations.contains_key("means_selection@0:0#1"));
    assert_eq!(
        run.usage()["tokens"]["input_tokens"],
        ports.calls.len() * 10
    );
}

#[test]
fn omitted_goal_criterion_retries_contract_without_repeating_intake() {
    let expected = new_run(settings());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        two_requirements: true,
        missing_criterion_once: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert!(run.error.as_ref().unwrap().message.contains("criteria"));
    let goal = run.goal["requirements"].clone();
    run.resume(&expected).unwrap();
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "completed");
    assert_eq!(run.goal["requirements"], goal);
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "goal_intake")
            .count(),
        1
    );
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| phase.as_str() == "goal_contract")
            .count(),
        2
    );
    assert!(run.generations.contains_key("goal_contract@0:0#0"));
    assert!(run.generations.contains_key("goal_contract@0:0#1"));
}

#[test]
fn partially_effectful_failed_generation_is_not_automatically_regenerated() {
    let expected = new_run(tool_settings());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        tool_mode: true,
        partial_tool_error: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(ports.effects, 1);
    let calls = ports.calls.len();
    let error = run.resume(&expected).unwrap_err();
    assert_eq!(error.code, "conflict");
    assert!(error.message.contains("already committed tool effects"));
    assert_eq!(ports.calls.len(), calls);
    assert_eq!(ports.effects, 1);
    assert_eq!(run.status, "quarantined");
}

mod actual_tool_engine {
    use super::*;
    use std::collections::BTreeMap;
    use tobkiri_pack_harness_tools::engine::{self, Artifact, Backend, Receipt, ToolError};

    /// This backend is an in-memory fixture, not Core storage or a host bypass.
    #[derive(Default)]
    struct Memory {
        artifacts: BTreeMap<String, Artifact>,
        receipts: BTreeMap<String, Receipt>,
        mutations: usize,
    }
    impl Backend for Memory {
        fn get(&mut self, path: &str) -> Result<Option<Artifact>, ToolError> {
            Ok(self.artifacts.get(path).cloned())
        }
        fn list(&mut self, prefix: &str, after: &str, limit: u32) -> Result<Vec<Value>, ToolError> {
            Ok(self
                .artifacts
                .values()
                .filter(|artifact| {
                    !artifact.deleted
                        && artifact.path.starts_with(prefix)
                        && artifact.path.as_str() > after
                })
                .take(limit as usize)
                .map(Artifact::metadata)
                .collect())
        }
        fn receipt(&mut self, id: &str) -> Result<Option<Receipt>, ToolError> {
            Ok(self.receipts.get(id).cloned())
        }
        fn commit(
            &mut self,
            id: &str,
            signature: &str,
            artifact: Artifact,
            result: Value,
        ) -> Result<Receipt, ToolError> {
            let receipt = Receipt {
                signature: signature.into(),
                result,
            };
            self.artifacts.insert(artifact.path.clone(), artifact);
            self.receipts.insert(id.into(), receipt.clone());
            self.mutations += 1;
            Ok(receipt)
        }
        fn now_ms(&mut self) -> Result<u64, ToolError> {
            Ok(1720000000123)
        }
        fn random(&mut self, length: u32) -> Result<Vec<u8>, ToolError> {
            Ok(vec![0xab; length as usize])
        }
    }
    struct RealPorts {
        models: FakePorts,
        store: Memory,
        tool_results: Vec<(Value, Value)>,
    }
    impl Ports for RealPorts {
        fn active(&mut self) -> Result<(), Failure> {
            self.models.active()
        }
        fn persist(&mut self, run: &Run) -> Result<(), Failure> {
            self.models.persist(run)
        }
        fn generate(
            &mut self,
            model: &str,
            messages: &[Value],
            phase: &str,
        ) -> Result<Generated, Failure> {
            let mut generated = self.models.generate(model, messages, phase)?;
            let mut envelope: Value = serde_json::from_str(&generated.text).unwrap();
            if phase == "means_selection" {
                envelope["result"]["selected_tools"] = json!([
                    "artifact.write",
                    "artifact.stat",
                    "artifact.read",
                    "calculator"
                ]);
            }
            if !envelope["tool_calls"].as_array().unwrap().is_empty() {
                envelope["tool_calls"] = json!([
                    {"id":"write","name":"artifact.write","arguments":{"path":"answer.md","content":"323"}},
                    {"id":"stat","name":"artifact.stat","arguments":{"path":"answer.md"}},
                    {"id":"read","name":"artifact.read","arguments":{"path":"answer.md"}},
                    {"id":"calculate","name":"calculator","arguments":{"operation":"multiply","operands":[17,19]}}
                ]);
            }
            generated.text = envelope.to_string();
            Ok(generated)
        }
        fn tool(&mut self, call: &Value) -> Result<Value, Failure> {
            let result = engine::invoke(&mut self.store, &call.to_string());
            self.tool_results.push((call.clone(), result.clone()));
            Ok(result)
        }
        fn verify_artifact(&mut self, path: &str) -> Result<Value, Failure> {
            let result = engine::invoke(
                &mut self.store,
                &json!({"name":"artifact.stat","arguments":{"path":path}}).to_string(),
            );
            if result["ok"] != true || result["result"]["exists"] != true {
                return Err(workflow::invalid("actual artifact stat failed"));
            }
            Ok(result["result"].clone())
        }
    }

    #[test]
    fn real_tool_parser_dispatch_artifacts_calculator_and_receipts_survive_resume() {
        let mut config = settings();
        config.tools = [
            "artifact.write",
            "artifact.stat",
            "artifact.read",
            "calculator",
        ]
        .iter()
        .map(|name| Tool {
            name: (*name).into(),
            description: "actual harness-tools engine operation".into(),
            parameters: json!({"type":"object"}),
        })
        .collect();
        let expected = new_run(config);
        let mut run = expected.clone();
        let mut ports = RealPorts {
            models: FakePorts {
                tool_mode: true,
                fail_after_tool: true,
                ..Default::default()
            },
            store: Memory::default(),
            tool_results: vec![],
        };
        while run.status == "running" {
            let _ = run.advance(&mut ports);
        }
        assert_eq!(run.status, "quarantined");
        assert_eq!(ports.tool_results.len(), 4);
        assert_eq!(ports.tool_results[2].1["result"]["content"], "323");
        assert_eq!(ports.tool_results[3].1["result"]["value"], 323);
        assert_eq!(
            ports.tool_results[1].1["result"]["digest"],
            engine::digest(b"323")
        );
        assert!(
            ports
                .tool_results
                .iter()
                .all(|(call, result)| call["id"].as_str().unwrap().len() == 64
                    && result["ok"] == true)
        );
        let bytes = serde_json::to_vec(ports.models.saved.as_ref().unwrap()).unwrap();
        let mut restored: Run = serde_json::from_slice(&bytes).unwrap();
        restored.resume(&expected).unwrap();
        for _ in 0..40 {
            if restored.status != "running" {
                break;
            }
            restored.advance(&mut ports).unwrap();
        }
        assert_eq!(restored.status, "completed");
        assert_eq!(ports.store.mutations, 1);
        assert_eq!(ports.tool_results.len(), 4);
        assert_eq!(
            restored.artifacts["answer.md"]["digest"],
            engine::digest(b"323")
        );
        assert_eq!(restored.usage()["cost_known"], false);
    }
}
