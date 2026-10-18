//! Read-only preparation performed by worker steps inside the selected Flow.
//! The input adapter never obtains a private Store handle or changes a Run.

use crate::workflow::{Failure, Run, invalid};
use serde_json::{Value, json};

#[derive(Debug, PartialEq, Eq)]
pub(super) struct Context {
    pub conversation: String,
    pub history_text: String,
}

pub(super) fn continuation(
    run: &Run,
    conversation: &str,
    answers: Option<&str>,
) -> Result<Context, Failure> {
    if run.conversation != conversation {
        return Err(invalid("continuation conversation changed"));
    }
    Ok(Context {
        conversation: run.conversation.clone(),
        history_text: run.conversation_input(answers)?,
    })
}

pub(super) fn regeneration(run: &Run) -> Result<Context, Failure> {
    Ok(Context {
        conversation: run.conversation.clone(),
        history_text: run.conversation_input(None)?,
    })
}

/// A normal Flow ends with its last call value. `return` is reserved for
/// overlays, so this pure worker forms the adapter's closed result envelope.
pub(super) fn control_result(operation: &str, serialized: &str) -> Result<String, Failure> {
    if !matches!(operation, "status" | "reconcile-generation") {
        return Err(invalid("unsupported DeepThink control operation"));
    }
    let value: Value = serde_json::from_str(serialized)
        .map_err(|_| invalid("DeepThink control result is not JSON"))?;
    Ok(json!({"op":operation, "result":value}).to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::workflow::Settings;

    fn fixture() -> Run {
        Run::new(
            "context-conversation".into(),
            "exact-model".into(),
            vec![
                json!({"role":"system","content":"Fixture system context"}),
                json!({"role":"user","content":"An earlier task"}),
                json!({"role":"assistant","content":"Earlier response"}),
                json!({"role":"user","content":"現在の依頼を保存する"}),
            ],
            Settings {
                max_review_rounds: 2,
                max_tool_rounds: 3,
                max_contract_retries: 0,
                structured_provider: false,
                tools: vec![],
                skills: vec![],
                profile_checks: vec![],
                environment: json!({"execution_mode":"verified_wasm",
                    "approval_policy":"core_gate","output_token_limit":"provider_default"}),
                capability_gaps: vec![],
            },
        )
        .unwrap()
    }

    fn accept_answer_fixture(run: &mut Run, answer: &str) {
        run.answers.push(answer.into());
        run.messages.push(json!({"role":"user", "content":
            format!("回答（既存Goalの未確認事項に対する補足）: {answer}")}));
    }

    #[test]
    fn continuation_uses_the_original_task_and_saved_conversation() {
        let run = fixture();
        let before = serde_json::to_vec(&run).unwrap();
        assert_eq!(
            continuation(&run, "context-conversation", None).unwrap(),
            Context {
                conversation: "context-conversation".into(),
                history_text: "現在の依頼を保存する".into(),
            }
        );
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
    }

    #[test]
    fn proposed_answer_is_prepared_without_mutating_pending_or_paid_state() {
        let mut run = fixture();
        run.status = "needs_input".into();
        run.attempts.insert(
            "goal_contract#0:1".into(),
            json!({"status":"pending", "usage":{"cost":"0.0001"},
                "partial":{"public_text":"retained prefix"}}),
        );
        accept_answer_fixture(&mut run, "読み手は技術者");
        let before = serde_json::to_vec(&run).unwrap();
        let context = continuation(&run, "context-conversation", Some("300字以内")).unwrap();
        assert_eq!(
            context.history_text,
            "現在の依頼を保存する\n\n確認事項への回答:\n読み手は技術者\n\n確認事項への回答:\n300字以内"
        );
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
        // Read-only preparation is not permission to resume the pending request.
        assert_eq!(
            run.ensure_generations_reconciled().unwrap_err().code,
            "needs-reconciliation"
        );
    }

    #[test]
    fn conversation_mismatch_is_rejected_without_rewriting_the_run() {
        let run = fixture();
        let before = serde_json::to_vec(&run).unwrap();
        assert_eq!(
            continuation(&run, "different-conversation", None)
                .unwrap_err()
                .message,
            "continuation conversation changed"
        );
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
    }

    #[test]
    fn proposed_answer_requires_a_nonempty_pending_clarification() {
        let mut run = fixture();
        assert!(continuation(&run, "context-conversation", Some("unexpected")).is_err());
        run.status = "needs_input".into();
        for empty in ["", " \n\t"] {
            assert!(continuation(&run, "context-conversation", Some(empty)).is_err());
        }
    }

    #[test]
    fn regeneration_keeps_accepted_history_and_never_authorizes_dispatch() {
        let mut run = fixture();
        accept_answer_fixture(&mut run, "公開資料のみ");
        run.status = "quarantined".into();
        run.attempts
            .insert("goal_contract#0:1".into(), json!({"status":"pending"}));
        let before = serde_json::to_vec(&run).unwrap();
        let context = regeneration(&run).unwrap();
        assert_eq!(context.conversation, "context-conversation");
        assert_eq!(
            context.history_text,
            "現在の依頼を保存する\n\n確認事項への回答:\n公開資料のみ"
        );
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
        assert_eq!(
            run.ensure_generations_reconciled().unwrap_err().code,
            "needs-reconciliation"
        );
    }

    #[test]
    fn corrupted_clarification_history_is_rejected_by_both_preparations() {
        let mut run = fixture();
        accept_answer_fixture(&mut run, "accepted");
        run.messages.last_mut().unwrap()["content"] = json!("rewritten answer");
        let before = serde_json::to_vec(&run).unwrap();
        assert!(continuation(&run, "context-conversation", None).is_err());
        assert!(regeneration(&run).is_err());
        assert_eq!(serde_json::to_vec(&run).unwrap(), before);
    }

    #[test]
    fn control_result_keeps_json_types_without_double_encoding() {
        for operation in ["status", "reconcile-generation"] {
            for value in [
                Value::Null,
                json!(true),
                json!(17),
                json!("public text"),
                json!([{"status":"paused"}, null]),
                json!({"nested":{"public_text":"公開文", "count":"9007199254740993"}}),
            ] {
                let envelope: Value =
                    serde_json::from_str(&control_result(operation, &value.to_string()).unwrap())
                        .unwrap();
                assert_eq!(envelope, json!({"op":operation,"result":value}));
            }
        }
    }

    #[test]
    fn control_result_cannot_select_an_unlisted_worker_operation() {
        for operation in ["", "regenerate-phase", "continue-run", "lookup", "STATUS"] {
            assert_eq!(
                control_result(operation, "null").unwrap_err().message,
                "unsupported DeepThink control operation"
            );
        }
    }

    #[test]
    fn malformed_control_result_is_rejected_instead_of_returning_partial_json() {
        for serialized in ["", "{", "null trailing", "{\"result\":"] {
            assert_eq!(
                control_result("status", serialized).unwrap_err().message,
                "DeepThink control result is not JSON"
            );
        }
    }
}
