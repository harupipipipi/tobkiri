use crate::{
    phases,
    workflow::{
        self, AttemptContext, Failure, Generated, GenerationProgress, Ports, Run, Settings, Tool,
    },
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

fn settings() -> Settings {
    Settings {
        max_review_rounds: 2,
        max_tool_rounds: 3,
        max_contract_retries: 0,
        structured_provider: false,
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

fn runtime_head(run: &Run) -> crate::run_repository::Head {
    crate::run_repository::Head {
        run_id: run.id.clone(),
        run_request_digest: crate::run_repository::Digest::parse(&run.request_digest).unwrap(),
        revision: 0,
        snapshot_digest: crate::run_repository::Digest::hash(&serde_json::to_vec(run).unwrap()),
    }
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
    claims: BTreeMap<String, AttemptContext>,
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
    declarative_tool_request_count: usize,
    captured_messages: Vec<(String, Vec<Value>)>,
    bad_selection_once: bool,
    missing_criterion_once: bool,
    two_requirements: bool,
    bad_generation_seen: bool,
    partial_tool_error: bool,
    invalid_public_output_once: Option<String>,
    first_rejection_snapshot: Option<Run>,
    invalid_public_output_count: usize,
    invalid_after_tool_once: bool,
    selection_gap: Option<Value>,
    repeat_tool_after_rejection: bool,
    progress_failure: bool,
    progress: BTreeMap<String, GenerationProgress>,
    persist_calls: usize,
    fail_persist_at: Option<usize>,
    snapshots: Vec<Run>,
}
impl Ports for FakePorts {
    fn claim_generation(&mut self, run: &Run, attempt: &AttemptContext) -> Result<(), Failure> {
        run.validate_generation_claim(attempt)?;
        let key = format!("{}:{}", attempt.run_id, attempt.generation_key);
        if self.claims.contains_key(&key) {
            return Err(Failure {
                code: "conflict".into(),
                message: "fixture duplicate durable claim".into(),
            });
        }
        self.persist(run)?;
        self.claims.insert(key, attempt.clone());
        Ok(())
    }
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
        attempt: &AttemptContext,
    ) -> Result<Generated, Failure> {
        self.calls.push(phase.into());
        self.captured_messages
            .push((phase.into(), messages.to_vec()));
        if self.progress_failure {
            let mut progress = GenerationProgress::new(attempt.clone());
            progress.accept(&json!({"kind":"metadata","text":json!({"version":"tobkiri.chat.metadata.v1","generation_id":"gen-fixture-123","model":"fixture-model","provider":"fixture-provider"}).to_string()}))?;
            progress.accept(&json!({"kind":"delta","text":"{\"summary\":"}))?;
            progress.accept(&json!({"kind":"usage","prompt-tokens":10,"completion-tokens":2}))?;
            progress.next_snapshot()?;
            self.progress.insert(attempt.attempt_id.clone(), progress);
            return Err(Failure {
                code: "unavailable".into(),
                message: "fixture stream interrupted".into(),
            });
        }
        if phase == "goal_intake" && self.declarative_tool_request_count > 0 {
            self.declarative_tool_request_count -= 1;
            return Ok(Generated {
                text: json!({"summary":summary(),"tool_calls":[{"id":"too-early","name":"artifact.write","arguments":{"path":"unexpected.md","content":"x"}}],"result":null}).to_string(),
                usage: json!({"input_tokens":10,"output_tokens":5,"cost_usd":"0.000001"}),
            });
        }
        if phase == "goal_intake"
            && (!self.bad_generation_seen || self.invalid_public_output_count > 0)
            && let Some(text) = self.invalid_public_output_once.clone()
        {
            self.bad_generation_seen = true;
            self.invalid_public_output_count = self.invalid_public_output_count.saturating_sub(1);
            return Ok(Generated {
                text,
                usage: json!({"input_tokens":10,"output_tokens":5,"cost_usd":"0.000001"}),
            });
        }
        let ctx = context(messages);
        self.contexts.push((phase.into(), ctx.clone()));
        let mut result = if workflow::goal_contract_gate::is_phase(phase) {
            if phase == "goal_contract_revision:1" {
                let mut contract = ctx["contract_candidate"].clone();
                contract["success_criteria"][0]["measurement"] =
                    json!("Fixture corrected measurement");
                json!({"contract":contract,"changes":["Fixture semantic correction"]})
            } else {
                workflow::goal_contract_gate::tests::approved_review(&ctx)
            }
        } else {
            fixture(phase)
        };
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
        if phase == "means_selection"
            && let Some(gap) = &self.selection_gap
        {
            result["gaps"] = json!([gap]);
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
                if !messages.iter().any(|message| {
                    message["content"]
                        .as_str()
                        .is_some_and(|text| text.starts_with("Verified real tool results:"))
                }) || (self.repeat_tool_after_rejection && self.bad_generation_seen)
                {
                    calls = json!([{"id":"write-one","name":"artifact.write","arguments":{"path":"answer.md","content":"323"}}]);
                    if self.partial_tool_error {
                        calls
                            .as_array_mut()
                            .unwrap()
                            .push(json!({"id":"unknown","name":"not-selected","arguments":{}}));
                    }
                    result = Value::Null;
                } else if self.invalid_after_tool_once && !self.bad_generation_seen {
                    self.bad_generation_seen = true;
                    return Ok(Generated {
                        text: "{".into(),
                        usage: json!({"input_tokens":10,"output_tokens":5,"cost_usd":"0.000001"}),
                    });
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
        // This deliberate provider violation must reach the real validator.
        if !(phase.starts_with("profile_check:") && self.tool_in_profile_check) {
            phases::validate(&envelope, &workflow::phase_schema(phase)).unwrap();
        }
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
    fn generation_progress(
        &mut self,
        attempt: &AttemptContext,
    ) -> Result<Option<GenerationProgress>, Failure> {
        Ok(self.progress.get(&attempt.attempt_id).cloned())
    }
    fn verify_artifact(&mut self, path: &str) -> Result<Value, Failure> {
        if self.effects == 0 {
            return Err(workflow::invalid("missing actual artifact"));
        }
        Ok(json!({"path":path,"kind":"tool-owned-storage-artifact","digest":"sha256:fixture"}))
    }
    fn persist(&mut self, run: &Run) -> Result<(), Failure> {
        self.persist_calls += 1;
        if self.fail_persist_at == Some(self.persist_calls) {
            return Err(Failure {
                code: "core-internal".into(),
                message: "fixture checkpoint commit unknown; do not replay".into(),
            });
        }
        if self.first_rejection_snapshot.is_none() && !run.rejected_generations.is_empty() {
            self.first_rejection_snapshot = Some(run.clone());
        }
        self.saved = Some(run.clone());
        self.snapshots.push(run.clone());
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
fn bounded_contract_retries_survive_restart_and_keep_all_rejections_and_costs() {
    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config);
    let mut ports = FakePorts {
        invalid_public_output_once: Some("{".into()),
        invalid_public_output_count: 2,
        ..Default::default()
    };
    let progress = run.advance(&mut ports).unwrap();
    assert_eq!(progress[0]["status"], "retrying");
    assert_eq!(run.status, "running");
    assert_eq!(run.contract_retries["goal_intake@0:0"], 1);
    let saved = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
    let mut restored: Run = serde_json::from_slice(&saved).unwrap();
    let progress = restored.advance(&mut ports).unwrap();
    assert_eq!(progress[0]["retry"], 2);
    complete(&mut restored, &mut ports);
    assert_eq!(restored.status, "completed");
    assert_eq!(restored.rejected_generations.len(), 2);
    assert_eq!(restored.contract_retries["goal_intake@0:0"], 2);
    assert!(restored.generations.contains_key("goal_intake@0:0#2"));
    assert_eq!(restored.usage()["attempts"], ports.calls.len());
    assert_eq!(
        restored.usage()["tokens"]["input_tokens"],
        ports.calls.len() * 10
    );
    assert_eq!(restored.usage()["cost_known"], true);
    assert_eq!(ports.effects, 0);
}

#[test]
fn automatic_contract_retry_limit_quarantines_without_fabricating_success() {
    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config);
    let mut ports = FakePorts {
        invalid_public_output_once: Some("{}".into()),
        invalid_public_output_count: 3,
        ..Default::default()
    };
    assert_eq!(run.advance(&mut ports).unwrap()[0]["retry"], 1);
    assert_eq!(run.advance(&mut ports).unwrap()[0]["retry"], 2);
    assert!(run.advance(&mut ports).is_err());
    assert_eq!(run.status, "quarantined");
    assert_eq!(run.output(), "");
    assert_eq!(run.rejected_generations.len(), 3);
    assert_eq!(run.usage()["attempts"], 3);
    assert_eq!(ports.calls.len(), 3);
    assert!(run.advance(&mut ports).unwrap().is_empty());
    assert_eq!(ports.calls.len(), 3);

    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config);
    let mut ports = FakePorts {
        bad_selection_once: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert!(run.contract_retries.is_empty());
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|p| p.as_str() == "means_selection")
            .count(),
        1
    );
}

#[test]
fn invalid_generation_after_tool_effect_needs_explicit_resume_without_effect_replay() {
    let mut config = tool_settings();
    config.max_contract_retries = 2;
    let expected = new_run(config.clone());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        tool_mode: true,
        invalid_after_tool_once: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert_eq!(ports.effects, 1);
    assert!(run.contract_retries.is_empty());
    assert!(
        run.rejected_generations
            .contains_key("section:calculation@0:1#0")
    );
    run.resume(&expected).unwrap();
    complete(&mut run, &mut ports);
    assert_eq!(run.status, "completed");
    assert_eq!(ports.effects, 1);
    assert_eq!(run.usage()["attempts"], ports.calls.len());

    let mut run = expected.clone();
    let mut ports = FakePorts {
        tool_mode: true,
        invalid_after_tool_once: true,
        repeat_tool_after_rejection: true,
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    run.resume(&expected).unwrap();
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert_eq!(run.error.as_ref().unwrap().code, "conflict");
    assert_eq!(ports.effects, 1);
}

#[test]
fn typed_optional_gaps_proceed_and_real_required_gaps_preserve_user_input_gate() {
    for blocking in [false, true] {
        let mut run = new_run(settings());
        let mut ports = FakePorts {
            selection_gap: Some(json!({
                "requirement_ids":["r1"],"blocking":blocking,
                "description":"専用計算ツールは未提供",
                "resolution":if blocking {"必要な専用検証手段を提供してください"} else {"この整数計算は手計算で検算する"}
            })),
            ..Default::default()
        };
        complete(&mut run, &mut ports);
        assert_eq!(
            run.status,
            if blocking { "needs_input" } else { "completed" }
        );
        assert_eq!(run.context["selection"]["gaps"][0]["blocking"], blocking);
        if blocking {
            assert!(run.output().contains("必要な専用検証手段"));
        }
    }
    let mut run = new_run(settings());
    let mut ports = FakePorts {
        selection_gap: Some(json!({
            "requirement_ids":["invented"],"blocking":true,
            "description":"未知の要件","resolution":"推測で範囲を追加しない"
        })),
        ..Default::default()
    };
    while run.status == "running" {
        let _ = run.advance(&mut ports);
    }
    assert_eq!(run.status, "quarantined");
    assert!(run.error.as_ref().unwrap().message.contains("actual Goal"));
    assert_eq!(ports.effects, 0);
}

#[test]
fn protocol_version_and_retry_bounds_do_not_silently_upgrade_old_checkpoints() {
    let expected = new_run(settings());
    let mut old = expected.clone();
    old.version = "deepthink.workflow.v2".into();
    assert!(old.resume(&expected).is_err());
    let mut config = settings();
    config.max_contract_retries = 4;
    assert!(config.validate().is_err());
    let mut document = serde_json::to_value(settings()).unwrap();
    document
        .as_object_mut()
        .unwrap()
        .remove("max_contract_retries");
    let compatible: Settings = serde_json::from_value(document).unwrap();
    assert_eq!(compatible.max_contract_retries, 2);
}

#[test]
fn malformed_and_schema_invalid_public_generations_are_durable_and_not_cached() {
    for public_output in [r#"{"summary":"#, "{}"] {
        let mut run = new_run(settings());
        let mut ports = FakePorts {
            invalid_public_output_once: Some(public_output.into()),
            ..Default::default()
        };
        assert!(run.advance(&mut ports).is_err());
        assert_eq!(run.status, "quarantined");
        assert!(run.generations.is_empty());
        assert_eq!(run.rejected_generations.len(), 1);
        let rejected = &run.rejected_generations["goal_intake@0:0#0"];
        assert_eq!(rejected.generated.text, public_output);
        assert_eq!(rejected.generated.usage["input_tokens"], 10);
        assert_eq!(rejected.generated.usage["cost_usd"], "0.000001");
        assert_eq!(rejected.request_digest.len(), 64);
        assert_eq!(run.usage()["attempts"], 1);
        assert_eq!(run.usage()["tokens_complete"], true);
        assert_eq!(run.usage()["cost_usd"], "0.000001");
        assert_eq!(run.output(), "");
        let saved = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
        let restored: Run = serde_json::from_slice(&saved).unwrap();
        assert_eq!(
            restored.rejected_generations["goal_intake@0:0#0"]
                .generated
                .text,
            public_output
        );
        assert!(restored.generations.is_empty());
    }

    // Checkpoints created before rejection records existed remain readable.
    let mut legacy = serde_json::to_value(new_run(settings())).unwrap();
    legacy
        .as_object_mut()
        .unwrap()
        .remove("rejected_generations");
    let restored: Run = serde_json::from_value(legacy).unwrap();
    assert!(restored.rejected_generations.is_empty());
}

#[test]
fn first_rejection_checkpoint_is_already_quarantined_and_resumes_without_overwrite() {
    let expected = new_run(settings());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        invalid_public_output_once: Some(r#"{"summary":"#.into()),
        ..Default::default()
    };
    assert!(run.advance(&mut ports).is_err());

    // This exact snapshot precedes advance()'s outer error-path persistence.
    // A process stopped in that interval must not reuse the rejection's key.
    let checkpoint = ports.first_rejection_snapshot.as_ref().unwrap();
    assert_eq!(checkpoint.status, "quarantined");
    assert_eq!(checkpoint.goal["status"], "quarantined");
    assert_eq!(
        checkpoint.error.as_ref().unwrap().code,
        "invalid-phase-contract"
    );
    assert!(checkpoint.entry.is_some());
    assert_eq!(checkpoint.attempts.len(), 1);
    assert!(checkpoint.generations.is_empty());
    let original_rejection =
        serde_json::to_value(&checkpoint.rejected_generations["goal_intake@0:0#0"]).unwrap();
    let bytes = serde_json::to_vec(checkpoint).unwrap();
    let mut restored: Run = serde_json::from_slice(&bytes).unwrap();
    restored.resume(&expected).unwrap();
    assert_eq!(restored.status, "running");
    assert_eq!(restored.generation_epochs["goal_intake@0:0"], 1);
    complete(&mut restored, &mut ports);

    assert_eq!(restored.status, "completed");
    assert_eq!(
        serde_json::to_value(&restored.rejected_generations["goal_intake@0:0#0"]).unwrap(),
        original_rejection
    );
    assert_eq!(restored.rejected_generations.len(), 1);
    assert!(restored.generations.contains_key("goal_intake@0:0#1"));
    assert!(!restored.generations.contains_key("goal_intake@0:0#0"));
    assert_eq!(
        ports
            .calls
            .iter()
            .filter(|phase| *phase == "goal_intake")
            .count(),
        2
    );
    assert_eq!(restored.usage()["attempts"], ports.calls.len());
    assert_eq!(
        restored.usage()["tokens"]["input_tokens"],
        ports.calls.len() * 10
    );
}

#[test]
fn same_phase_resume_regenerates_rejected_outputs_and_keeps_prior_rejections_and_costs() {
    for public_output in [r#"{"summary":"#, "{}"] {
        let expected = new_run(settings());
        let mut run = expected.clone();
        let mut ports = FakePorts {
            invalid_public_output_once: Some(public_output.into()),
            ..Default::default()
        };
        assert!(run.advance(&mut ports).is_err());
        let saved = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
        let mut restored: Run = serde_json::from_slice(&saved).unwrap();
        restored.resume(&expected).unwrap();
        assert_eq!(restored.generation_epochs["goal_intake@0:0"], 1);

        // A second rejected response gets a separate durable reference, rather
        // than replacing the first rejection or reusing it as a generation.
        ports.bad_generation_seen = false;
        assert!(restored.advance(&mut ports).is_err());
        assert_eq!(restored.rejected_generations.len(), 2);
        assert!(restored.generations.is_empty());
        assert_eq!(restored.usage()["attempts"], 2);
        restored.resume(&expected).unwrap();
        assert_eq!(restored.generation_epochs["goal_intake@0:0"], 2);
        complete(&mut restored, &mut ports);

        assert_eq!(restored.status, "completed");
        assert_eq!(
            ports
                .calls
                .iter()
                .filter(|phase| *phase == "goal_intake")
                .count(),
            3
        );
        for key in ["goal_intake@0:0#0", "goal_intake@0:0#1"] {
            assert_eq!(
                restored.rejected_generations[key].generated.text,
                public_output
            );
            assert!(!restored.generations.contains_key(key));
        }
        assert!(restored.generations.contains_key("goal_intake@0:0#2"));
        assert_eq!(restored.usage()["attempts"], ports.calls.len());
        assert_eq!(
            restored.usage()["tokens"]["input_tokens"],
            ports.calls.len() * 10
        );
        assert_eq!(restored.usage()["tokens_complete"], true);
        assert_eq!(restored.usage()["cost_known"], true);
    }
}

#[test]
fn unresolved_transport_attempt_blocks_resume_and_preserves_completed_tool_receipts() {
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
    let calls_before_resume = ports.calls.len();
    assert_eq!(
        restored.resume(&expected).unwrap_err().code,
        "needs-reconciliation"
    );
    assert_eq!(restored.status, "quarantined");
    assert_eq!(ports.calls.len(), calls_before_resume);
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
    assert_eq!(
        restored
            .tool_receipts
            .values()
            .filter(|receipt| receipt.status == "completed")
            .count(),
        1
    );
    assert!(restored.output().is_empty());
}

#[test]
fn interrupted_public_progress_survives_restore_but_never_becomes_a_phase_cache_hit() {
    let expected = new_run(settings());
    let mut run = expected.clone();
    let mut ports = FakePorts {
        progress_failure: true,
        ..Default::default()
    };
    assert!(run.advance(&mut ports).is_err());
    let original_inputs = run.phase_inputs.clone();
    let bytes = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
    let mut restored: Run = serde_json::from_slice(&bytes).unwrap();
    let (id, attempt) = restored.attempts.iter().next().unwrap();
    assert_eq!(
        attempt["progress"]["metadata"]["generation_id"],
        "gen-fixture-123"
    );
    assert_eq!(attempt["progress"]["public_text"], "{\"summary\":");
    assert_eq!(attempt["context"]["attempt_id"], *id);
    assert_eq!(
        attempt["context"]["request_digest"].as_str().unwrap().len(),
        64
    );
    assert_eq!(
        attempt["context"]["route_digest"].as_str().unwrap().len(),
        64
    );
    assert_eq!(restored.usage()["tokens"]["input_tokens"], 10);
    assert_eq!(restored.usage()["tokens_complete"], false);
    assert_eq!(restored.usage()["cost_known"], false);
    assert!(restored.generations.is_empty());
    assert!(restored.rejected_generations.is_empty());
    assert!(restored.output().is_empty());
    assert_eq!(
        restored.resume(&expected).unwrap_err().code,
        "needs-reconciliation"
    );
    assert_eq!(restored.phase_inputs, original_inputs);
    // Even callers forcing the in-memory status cannot cause another POST.
    restored.status = "running".into();
    assert!(restored.advance(&mut ports).is_err());
    assert_eq!(ports.calls.len(), 1);
}

#[test]
fn legacy_pending_and_completed_attempt_without_public_generation_cannot_replay() {
    for status in ["pending", "completed"] {
        let expected = new_run(settings());
        let mut run = expected.clone();
        run.version = "deepthink.workflow.v3".into();
        run.attempts.insert(
            "goal_intake@0:0#0:0".into(),
            json!({"phase":"goal_intake","status":status,"usage":null}),
        );
        let before = serde_json::to_value(&run).unwrap();
        assert_eq!(
            run.resume(&expected).unwrap_err().code,
            "needs-reconciliation"
        );
        assert_eq!(serde_json::to_value(&run).unwrap(), before);
    }
}

#[test]
fn every_completed_attempt_and_its_public_generation_share_one_checkpoint() {
    for invalid in [false, true] {
        let mut run = new_run(settings());
        let mut ports = FakePorts {
            invalid_public_output_once: invalid.then(|| "{".into()),
            ..Default::default()
        };
        let _ = run.advance(&mut ports);
        for snapshot in &ports.snapshots {
            for attempt in snapshot
                .attempts
                .values()
                .filter(|attempt| attempt["status"] == "completed")
            {
                let key = attempt["context"]["generation_key"].as_str().unwrap();
                assert!(
                    snapshot.generations.contains_key(key)
                        || snapshot.rejected_generations.contains_key(key)
                );
            }
        }
    }
}

#[test]
fn unconfirmed_checkpoint_never_retries_a_paid_request_or_writes_over_an_unknown_commit() {
    for (at, invalid) in [(3, false), (4, false), (4, true)] {
        let mut config = settings();
        config.max_contract_retries = 3;
        let mut run = new_run(config);
        let mut ports = FakePorts {
            fail_persist_at: Some(at),
            invalid_public_output_once: invalid.then(|| "{".into()),
            ..Default::default()
        };
        let error = run.advance(&mut ports).unwrap_err();
        assert_eq!(error.code, "checkpoint-unconfirmed");
        assert_eq!(ports.persist_calls, at);
        assert_eq!(ports.calls.len(), usize::from(at == 4));
        assert!(run.contract_retries.is_empty());
        if at == 4 {
            let saved = ports.saved.as_ref().unwrap();
            assert_eq!(
                saved.ensure_generations_reconciled().unwrap_err().code,
                "needs-reconciliation"
            );
        }
    }
    let internal = crate::failure(crate::Error {
        code: crate::ErrorCode::Internal,
        message: "unconfirmed commit".into(),
    });
    assert_eq!(internal.code, "core-internal");
}

#[test]
fn progress_keeps_only_public_fields_and_rejects_route_identity_or_prefix_changes() {
    let mut run = new_run(settings());
    let mut ports = FakePorts {
        progress_failure: true,
        ..Default::default()
    };
    let _ = run.advance(&mut ports);
    let mut progress = ports.progress.values().next().unwrap().clone();
    let original = serde_json::to_value(&run).unwrap();
    progress.attempt.route_digest = "different-route".into();
    assert!(run.merge_generation_progress(progress).is_err());
    assert_eq!(serde_json::to_value(&run).unwrap(), original);
    let mut progress = ports.progress.values().next().unwrap().clone();
    progress.public_text = "changed".into();
    assert!(run.merge_generation_progress(progress).is_err());
    let mut progress = ports.progress.values().next().unwrap().clone();
    progress.next_snapshot().unwrap();
    progress.usage["input_tokens"] = json!(9);
    assert!(run.merge_generation_progress(progress).is_err());
    let mut progress = ports.progress.values().next().unwrap().clone();
    progress.metadata["reasoning"] = json!("private-marker");
    assert!(run.merge_generation_progress(progress).is_err());
    let mut progress = ports.progress.values().next().unwrap().clone();
    progress
        .accept(&json!({"kind":"progress","text":"private-marker-must-not-persist"}))
        .unwrap();
    assert!(
        !serde_json::to_string(&progress)
            .unwrap()
            .contains("private-marker")
    );
    assert!(progress.accept(&json!({"kind":"metadata","text":json!({"version":"tobkiri.chat.metadata.v1","reasoning":"private-marker"}).to_string()})).is_err());
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
fn structured_generation_port_carries_actual_schema_as_json_and_never_falls_back_implicitly() {
    let messages = vec![json!({"role":"user","content":"課題"})];
    let (port, arguments) =
        crate::generation_port_arguments("fixture-model", &messages, "goal_intake", true);
    assert_eq!(port, "local.harness-chat.structured-llm-provider@1.0");
    assert_eq!(arguments["prompt"]["max-tokens"], 0);
    assert_eq!(
        arguments["response-format"]["schema"],
        phases::schema("goal_intake")
    );
    assert!(arguments["response-format"]["schema"].is_object());
    assert_eq!(arguments["response-format"]["strict"], true);
    assert!(arguments["response-format"]["name"].as_str().unwrap().len() < 64);
    let (port, arguments) =
        crate::generation_port_arguments("fixture-model", &messages, "goal_intake", false);
    assert_eq!(port, "local.harness-chat.llm-provider@1.0");
    assert!(arguments.get("response-format").is_none());
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
                head: runtime_head(&run),
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
    let error = crate::register(run, None).unwrap_err();
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
    assert_eq!(run.error.as_ref().unwrap().code, "invalid-phase-contract");
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
fn declarative_contracts_require_empty_tools_and_a_result_for_every_phase() {
    let declarative = [
        "goal_intake",
        "quality_terms:1",
        "goal_contract",
        "preflight",
        "answer_plan",
        "capability_assessment",
        "means_selection",
        "profile_check:local",
        "reference_plan",
    ];
    let work = [
        "work_environment",
        "reference_search",
        "evidence",
        "section:calculation",
        "integration",
        "review:1",
        "revision:1",
    ];
    for phase in declarative.into_iter().chain(work) {
        let schema = phases::schema(phase);
        let result = fixture(phase);
        let valid = json!({"summary":summary(),"tool_calls":[],"result":result});
        phases::validate(&valid, &schema).unwrap();
        let requested = json!({"summary":summary(),"tool_calls":[{"id":"one","name":"artifact.write","arguments":{}}],"result":null});
        if declarative.contains(&phase) {
            assert!(!phases::allows_tool_execution(phase));
            assert_eq!(schema["properties"]["tool_calls"]["maxItems"], 0);
            assert!(phases::validate(&requested, &schema).is_err());
            let missing_result = json!({"summary":summary(),"tool_calls":[],"result":null});
            assert!(phases::validate(&missing_result, &schema).is_err());
        } else {
            assert!(phases::allows_tool_execution(phase));
            phases::validate(&requested, &schema).unwrap();
        }
    }
}

#[test]
fn forbidden_goal_tool_requests_are_durable_bounded_corrections_without_effects() {
    let mut config = settings();
    config.max_contract_retries = 2;
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        declarative_tool_request_count: 1,
        ..Default::default()
    };
    assert_eq!(
        run.advance_segment(&mut ports).unwrap()[0]["status"],
        "retrying"
    );
    let checkpoint = ports.first_rejection_snapshot.as_ref().unwrap();
    assert_eq!(checkpoint.status, "quarantined");
    assert_eq!(
        checkpoint.error.as_ref().unwrap().code,
        "invalid-phase-contract"
    );
    assert_eq!(
        checkpoint.attempts.values().next().unwrap()["status"],
        "completed"
    );
    assert!(checkpoint.generations.is_empty());
    let rejected = serde_json::to_value(&run.rejected_generations).unwrap();
    let old_inputs = run.phase_inputs.clone();
    let saved = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
    let mut restored: Run = serde_json::from_slice(&saved).unwrap();
    restored.advance_segment(&mut ports).unwrap();
    assert_eq!(restored.status, "paused");
    assert_eq!(restored.phases.len(), 1);
    assert_eq!(restored.phases[0]["phase"], "goal_intake");
    assert_eq!(
        serde_json::to_value(&restored.rejected_generations).unwrap(),
        rejected
    );
    assert_eq!(restored.phase_inputs, old_inputs);
    assert_eq!(restored.usage()["attempts"], 2);
    assert_eq!(restored.usage()["pending_attempts"], 0);
    assert_eq!(restored.usage()["tokens"]["input_tokens"], 20);
    assert_eq!(restored.usage()["tokens"]["output_tokens"], 10);
    assert_eq!(ports.effects, 0);
    let retry = &restored.retry_contexts["goal_intake@0:0#1"];
    assert_eq!(retry["phase_contract"], phases::schema("goal_intake"));
    assert_eq!(
        retry["phase_execution_policy"],
        phases::execution_policy("goal_intake")
    );
    let messages = &ports.captured_messages.last().unwrap().1;
    assert!(
        messages[0]["content"]
            .as_str()
            .unwrap()
            .contains("This is a declarative phase")
    );
    assert!(
        messages.last().unwrap()["content"]
            .as_str()
            .unwrap()
            .contains("phase_contract")
    );

    let mut exhausted = new_run(config);
    let mut ports = FakePorts {
        declarative_tool_request_count: 3,
        ..Default::default()
    };
    assert_eq!(
        exhausted.advance_segment(&mut ports).unwrap()[0]["retry"],
        1
    );
    assert_eq!(
        exhausted.advance_segment(&mut ports).unwrap()[0]["retry"],
        2
    );
    assert_eq!(
        exhausted.advance_segment(&mut ports).unwrap_err().code,
        "invalid-phase-contract"
    );
    assert_eq!(exhausted.status, "quarantined");
    assert_eq!(exhausted.rejected_generations.len(), 3);
    assert_eq!(exhausted.usage()["attempts"], 3);
    assert_eq!(ports.effects, 0);
    assert_eq!(ports.calls.len(), 3);
    assert!(exhausted.output().is_empty());
}

#[test]
fn a_legacy_known_declarative_failure_resumes_without_rewriting_its_receipts() {
    let config = settings();
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        declarative_tool_request_count: 1,
        ..Default::default()
    };
    run.advance_segment(&mut ports).unwrap_err();
    // The former worker saved a completed generation before its generic Invalid
    // phase guard. Recreate that historical state without changing its receipt.
    let key = "goal_intake@0:0#0";
    let mut generation = run.rejected_generations.remove(key).unwrap();
    let mut old_schema = phases::schema("goal_intake");
    old_schema["properties"]["tool_calls"]
        .as_object_mut()
        .unwrap()
        .remove("maxItems");
    old_schema["properties"]["result"] =
        json!({"oneOf":[old_schema["properties"]["result"].clone(),{"type":"null"}]});
    let inputs = run.phase_inputs.get_mut("goal_intake@0").unwrap();
    let ctx = context(inputs);
    inputs[0]["content"] = json!("Legacy general phase instruction with a tool request lane.");
    *inputs.last_mut().unwrap() = json!({"role":"user","content":format!("Legacy goal prompt\n\nphase:goal_intake\n\nschema:{old_schema}\n\ncontext:{ctx}")});
    let old_digest =
        workflow::digest(&json!({"model":run.model,"messages":inputs,"schema":old_schema}));
    generation.request_digest = old_digest.clone();
    let attempt_id = format!("{key}:0");
    run.attempts.get_mut(&attempt_id).unwrap()["context"]["request_digest"] = json!(old_digest);
    run.generations.insert(key.into(), generation);
    run.error = Some(Failure {
        code: "invalid".into(),
        message: "this declarative phase does not execute tools".into(),
    });
    let retained =
        serde_json::to_value((&run.attempts, &run.generations, &run.phase_inputs)).unwrap();
    let before_usage = run.usage();
    let mut unknown = run.clone();
    unknown.attempts.get_mut(&attempt_id).unwrap()["status"] = json!("pending");
    assert_eq!(
        unknown
            .resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
    assert!(unknown.generation_epochs.is_empty());
    let saved = serde_json::to_vec(&run).unwrap();
    let mut restored: Run = serde_json::from_slice(&saved).unwrap();
    restored
        .resume_pinned("fixture-conversation", "fixture-model", &config)
        .unwrap();
    assert_eq!(
        serde_json::to_value((
            &restored.attempts,
            &restored.generations,
            &restored.phase_inputs
        ))
        .unwrap(),
        retained
    );
    assert_eq!(restored.usage(), before_usage);
    restored.advance_segment(&mut ports).unwrap();
    assert_eq!(restored.status, "paused");
    assert_eq!(restored.phases.len(), 1);
    assert_eq!(restored.attempts[&attempt_id], run.attempts[&attempt_id]);
    assert_eq!(
        serde_json::to_value(&restored.generations[key]).unwrap(),
        serde_json::to_value(&run.generations[key]).unwrap()
    );
    assert_eq!(restored.phase_inputs, run.phase_inputs);
    assert_eq!(ports.calls.len(), 2);
    assert_eq!(ports.effects, 0);
    let correction = ports.captured_messages.last().unwrap().1.last().unwrap()["content"]
        .as_str()
        .unwrap();
    assert!(correction.contains("phase_execution_policy") && correction.contains("phase_contract"));
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

#[test]
fn completed_phase_and_pause_are_one_checkpoint_then_only_explicit_continuation_advances() {
    let config = settings();
    let mut run = new_run(config.clone());
    let mut ports = FakePorts::default();
    let events = run.advance_segment(&mut ports).unwrap();
    assert_eq!(events[0]["phase"], "goal_intake");
    assert_eq!(run.status, "paused");
    assert!(run.entry.is_none());
    assert!(run.output().is_empty());
    assert_eq!(ports.calls, ["goal_intake"]);
    assert!(
        ports
            .snapshots
            .iter()
            .filter(|snapshot| !snapshot.phases.is_empty())
            .all(|snapshot| snapshot.status == "paused")
    );
    let receipt = run.pause_receipt().unwrap();
    assert_eq!(receipt["approved"], false);
    assert_eq!(receipt["next_phase"], "goal_contract");
    assert_eq!(receipt["usage"]["pending_attempts"], 0);
    assert!(receipt.get("output").is_none());
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(ports.calls.len(), 1);
    let bytes = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
    let mut restored: Run = serde_json::from_slice(&bytes).unwrap();
    restored
        .resume_pinned("fixture-conversation", "fixture-model", &config)
        .unwrap();
    assert_eq!(restored.usage(), run.usage());
    restored.advance_segment(&mut ports).unwrap();
    assert_eq!(restored.status, "paused");
    assert_eq!(ports.calls, ["goal_intake", "goal_contract"]);
    assert_eq!(restored.phases.len(), 2);
    assert_eq!(restored.usage()["tokens"]["input_tokens"], 20);
    assert_eq!(restored.usage()["cost_usd"], "0.000002");
}

#[test]
fn separate_user_segments_keep_every_phase_criteria_review_and_exact_usage() {
    let config = tool_settings();
    let mut baseline = new_run(config.clone());
    let mut baseline_ports = FakePorts {
        tool_mode: true,
        ..Default::default()
    };
    complete(&mut baseline, &mut baseline_ports);
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        tool_mode: true,
        ..Default::default()
    };
    let mut pauses = 0;
    for _ in 0..60 {
        if run.status == "completed" {
            break;
        }
        if run.status == "paused" {
            pauses += 1;
            let bytes = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
            run = serde_json::from_slice(&bytes).unwrap();
            let usage = run.usage();
            run.resume_pinned("fixture-conversation", "fixture-model", &config)
                .unwrap();
            assert_eq!(run.usage(), usage);
        }
        run.advance_segment(&mut ports).unwrap();
    }
    assert_eq!(run.status, "completed");
    assert_eq!(run.phases, baseline.phases);
    assert_eq!(run.output(), baseline.output());
    assert_eq!(run.usage(), baseline.usage());
    assert_eq!(ports.calls, baseline_ports.calls);
    assert_eq!(ports.effects, 1);
    assert_eq!(run.review_rounds, 1);
    assert_eq!(pauses, run.phases.len() - 1);
    assert!(run.pause_receipt().is_err());
}

#[test]
fn a_schema_retry_spends_no_completed_phase_until_a_valid_result_is_saved() {
    let mut config = settings();
    config.max_contract_retries = 1;
    let mut run = new_run(config);
    let mut ports = FakePorts {
        invalid_public_output_once: Some("{".into()),
        ..Default::default()
    };
    let retry = run.advance_segment(&mut ports).unwrap();
    assert_eq!(retry[0]["status"], "retrying");
    assert_eq!(run.status, "running");
    assert!(run.phases.is_empty());
    assert!(run.pause_receipt().is_err());
    run.advance_segment(&mut ports).unwrap();
    assert_eq!(run.status, "paused");
    assert_eq!(run.phases.len(), 1);
    assert_eq!(ports.calls, ["goal_intake", "goal_intake"]);
    assert_eq!(run.rejected_generations.len(), 1);
    assert_eq!(run.usage()["attempts"], 2);
    assert_eq!(run.usage()["cost_usd"], "0.000002");
}

#[test]
fn pending_model_attempt_never_becomes_paused_or_a_fresh_segment_request() {
    let config = settings();
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        progress_failure: true,
        ..Default::default()
    };
    run.advance_segment(&mut ports).unwrap_err();
    assert_eq!(run.status, "quarantined");
    assert_eq!(run.usage()["cost_known"], false);
    assert!(run.pause_receipt().is_err());
    assert_eq!(
        run.resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
    assert!(run.advance_segment(&mut ports).unwrap().is_empty());
    assert_eq!(ports.calls, ["goal_intake"]);
    assert_eq!(run.attempts.len(), 1);
    assert!(run.generations.is_empty());
}

#[test]
fn an_unconfirmed_boundary_write_publishes_no_pause_and_makes_no_replacement_write() {
    let config = settings();
    let mut run = new_run(config.clone());
    let mut ports = FakePorts {
        fail_persist_at: Some(5),
        ..Default::default()
    };
    let error = run.advance_segment(&mut ports).unwrap_err();
    assert_eq!(error.code, "checkpoint-unconfirmed");
    assert_eq!(run.status, "quarantined");
    assert!(run.pause_receipt().is_err());
    assert!(run.output().is_empty());
    assert_eq!(ports.persist_calls, 5);
    assert_eq!(ports.calls, ["goal_intake"]);
    assert_eq!(run.usage()["attempts"], 1);
    assert_eq!(
        run.resume_pinned("fixture-conversation", "fixture-model", &config)
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
    assert_eq!(ports.persist_calls, 5);
}

#[test]
fn uncertain_receipts_or_changed_binding_cannot_resume_a_paused_checkpoint() {
    let config = settings();
    let mut run = new_run(config.clone());
    run.advance_segment(&mut FakePorts::default()).unwrap();
    for code in ["checkpoint-unconfirmed", "core-internal"] {
        let mut uncertain = run.clone();
        uncertain.error = Some(Failure {
            code: code.into(),
            message: "uncertain operation".into(),
        });
        assert!(uncertain.pause_receipt().is_err());
        assert_eq!(
            uncertain
                .resume_pinned("fixture-conversation", "fixture-model", &config)
                .unwrap_err()
                .code,
            "needs-reconciliation"
        );
    }
    for status in ["pending", "completed"] {
        let mut uncertain = run.clone();
        uncertain.tool_receipts.insert(
            "unknown-tool".into(),
            workflow::ToolReceipt {
                request_digest: "0".repeat(64),
                status: status.into(),
                outcome: None,
            },
        );
        assert!(uncertain.pause_receipt().is_err());
        assert!(
            uncertain
                .resume_pinned("fixture-conversation", "fixture-model", &config)
                .is_err()
        );
        assert_eq!(uncertain.status, "paused");
    }
    let mut changed = config.clone();
    changed.environment["provider_revision"] = json!("different");
    assert!(
        run.resume_pinned("fixture-conversation", "fixture-model", &changed)
            .is_err()
    );
    assert!(
        run.resume_pinned("another", "fixture-model", &config)
            .is_err()
    );
    assert_eq!(run.status, "paused");
}

#[test]
fn needs_input_and_review_rejection_keep_their_own_terminal_meanings() {
    let config = settings();
    let mut ambiguous = new_run(config.clone());
    let mut ports = FakePorts {
        terms: true,
        ambiguous: true,
        ..Default::default()
    };
    ambiguous.advance_segment(&mut ports).unwrap();
    ambiguous
        .resume_pinned("fixture-conversation", "fixture-model", &config)
        .unwrap();
    ambiguous.advance_segment(&mut ports).unwrap();
    assert_eq!(ambiguous.status, "needs_input");
    assert!(ambiguous.pause_receipt().is_err());
    assert!(!ambiguous.output().is_empty());
    let mut rejected = new_run(config.clone());
    let mut ports = FakePorts {
        reject: true,
        ..Default::default()
    };
    for _ in 0..60 {
        if rejected.status == "paused" {
            rejected
                .resume_pinned("fixture-conversation", "fixture-model", &config)
                .unwrap();
        }
        if rejected.advance_segment(&mut ports).is_err() {
            break;
        }
    }
    assert_eq!(rejected.status, "quarantined");
    assert_eq!(rejected.error.as_ref().unwrap().code, "review-rejected");
    assert_eq!(rejected.review_rounds, 2);
    assert!(rejected.output().is_empty());
    assert!(rejected.pause_receipt().is_err());
}

#[test]
fn paused_stream_is_an_explicit_boundary_with_no_candidate_delta_or_done() {
    let mut run = new_run(settings());
    run.advance_segment(&mut FakePorts::default()).unwrap();
    run.candidate = "unapproved-private-candidate".into();
    let usage = run.usage();
    let mut active = crate::Active {
        head: runtime_head(&run),
        run,
        events: Default::default(),
        announced: false,
        terminal_sent: false,
        failure: None,
    };
    let events = active.next(64).unwrap();
    let values = events
        .iter()
        .map(|event| serde_json::from_str::<Value>(event).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        values
            .iter()
            .map(|event| event["kind"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["usage", "progress", "paused"]
    );
    assert!(
        events
            .iter()
            .all(|event| !event.contains("unapproved-private-candidate"))
    );
    let receipt: Value = serde_json::from_str(values[2]["text"].as_str().unwrap()).unwrap();
    assert_eq!(receipt["usage"], usage);
    assert_eq!(receipt["approved"], false);
    assert!(active.next(64).unwrap().is_empty());
}

#[test]
fn a_new_version_cannot_sidestep_the_same_input_pending_v4_run() {
    let expected = new_run(settings());
    let mut previous = expected.clone();
    previous.version = "deepthink.workflow.v4".into();
    previous.id = expected.previous_v4_storage_id();
    previous.request_digest = previous.id.clone();
    previous.attempts.insert(
        "goal_intake@0:0#0:0".into(),
        json!({"phase":"goal_intake","status":"pending","usage":null}),
    );
    let saved = serde_json::to_vec(&previous).unwrap();
    assert_ne!(expected.id, previous.id);
    assert_eq!(
        expected.previous_v4_conflict(&previous).code,
        "needs-reconciliation"
    );
    assert_eq!(serde_json::to_vec(&previous).unwrap(), saved);
    assert_eq!(
        previous
            .resume_pinned("fixture-conversation", "fixture-model", &expected.settings)
            .unwrap_err()
            .code,
        "needs-reconciliation"
    );
    assert_eq!(serde_json::to_vec(&previous).unwrap(), saved);
    previous.attempts.clear();
    let saved = serde_json::to_vec(&previous).unwrap();
    assert_eq!(expected.previous_v4_conflict(&previous).code, "conflict");
    assert!(
        previous
            .resume_pinned("fixture-conversation", "fixture-model", &expected.settings)
            .is_err()
    );
    assert_eq!(serde_json::to_vec(&previous).unwrap(), saved);
}

#[test]
fn final_conversation_input_is_the_original_task_after_multiple_paused_continuations() {
    let config = settings();
    let mut run = new_run(config.clone());
    let original = run.messages.last().unwrap()["content"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut ports = FakePorts::default();
    let mut continuations = 0;
    for _ in 0..60 {
        if run.status == "completed" {
            break;
        }
        if run.status == "paused" {
            continuations += 1;
            assert_eq!(run.conversation_input(None).unwrap(), original);
            let bytes = serde_json::to_vec(ports.saved.as_ref().unwrap()).unwrap();
            run = serde_json::from_slice(&bytes).unwrap();
            run.resume_pinned("fixture-conversation", "fixture-model", &config)
                .unwrap();
        }
        run.advance_segment(&mut ports).unwrap();
    }
    assert_eq!(run.status, "completed");
    assert!(continuations >= 2);
    assert_eq!(run.conversation_input(None).unwrap(), original);
    assert!(
        !run.conversation_input(None)
            .unwrap()
            .contains("[DeepThink 再開")
    );
}

#[test]
fn accepted_clarifications_remain_with_the_original_task_in_the_final_user_turn() {
    let config = settings();
    let mut run = new_run(config.clone());
    let original = run.messages.last().unwrap()["content"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut ports = FakePorts {
        pause_preflight: true,
        ..Default::default()
    };
    for _ in 0..10 {
        if run.status == "needs_input" {
            break;
        }
        if run.status == "paused" {
            run.resume_pinned("fixture-conversation", "fixture-model", &config)
                .unwrap();
        }
        run.advance_segment(&mut ports).unwrap();
    }
    assert_eq!(run.status, "needs_input");
    let expected = format!("{original}\n\n確認事項への回答:\n技術者向け");
    assert_eq!(
        run.conversation_input(Some("技術者向け")).unwrap(),
        expected
    );
    run.answer("fixture-conversation", "技術者向け", &config)
        .unwrap();
    assert_eq!(run.conversation_input(None).unwrap(), expected);
    run.advance_segment(&mut ports).unwrap();
    assert_eq!(run.status, "paused");
    assert_eq!(run.conversation_input(None).unwrap(), expected);
    assert!(run.conversation_input(Some("unsolicited answer")).is_err());
    let mut tampered = run.clone();
    tampered.messages.last_mut().unwrap()["content"] = json!("different answer");
    assert!(tampered.conversation_input(None).is_err());
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
        fn claim_generation(&mut self, run: &Run, attempt: &AttemptContext) -> Result<(), Failure> {
            self.models.claim_generation(run, attempt)
        }
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
            attempt: &AttemptContext,
        ) -> Result<Generated, Failure> {
            let mut generated = self.models.generate(model, messages, phase, attempt)?;
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
    fn explicit_phase_segments_keep_real_tool_effects_once_with_original_receipts() {
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
        let mut run = new_run(config.clone());
        let mut ports = RealPorts {
            models: FakePorts {
                tool_mode: true,
                ..Default::default()
            },
            store: Memory::default(),
            tool_results: vec![],
        };
        for _ in 0..60 {
            if run.status == "completed" {
                break;
            }
            if run.status == "paused" {
                let bytes = serde_json::to_vec(ports.models.saved.as_ref().unwrap()).unwrap();
                let usage = run.usage();
                run = serde_json::from_slice(&bytes).unwrap();
                run.resume_pinned("fixture-conversation", "fixture-model", &config)
                    .unwrap();
                assert_eq!(run.usage(), usage);
            }
            run.advance_segment(&mut ports).unwrap();
        }
        assert_eq!(run.status, "completed");
        assert_eq!(ports.store.mutations, 1);
        assert_eq!(ports.store.receipts.len(), 1);
        assert_eq!(ports.tool_results.len(), 4);
        assert_eq!(ports.tool_results[2].1["result"]["content"], "323");
        assert_eq!(ports.tool_results[3].1["result"]["value"], 323);
        assert_eq!(run.tool_receipts.len(), 4);
        assert!(
            run.tool_receipts
                .values()
                .all(|receipt| receipt.status == "completed")
        );
        assert_eq!(run.usage()["attempts"], ports.models.calls.len());
        assert_eq!(
            run.usage()["tokens"]["input_tokens"],
            ports.models.calls.len() * 10
        );
        assert_eq!(run.review_rounds, 1);
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
                ..Default::default()
            },
            store: Memory::default(),
            tool_results: vec![],
        };
        while run.status == "running" && !run.artifacts.contains_key("answer.md") {
            run.advance(&mut ports).unwrap();
        }
        assert_eq!(run.status, "running");
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
        assert_eq!(restored.usage()["cost_known"], true);
    }
}
