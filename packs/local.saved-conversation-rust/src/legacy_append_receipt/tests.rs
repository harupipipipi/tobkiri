use super::*;
use ordered_python_compat::loads;

const USER_ID: &str = "message:aafefe94aea9eebdc30da09870cf57e2b3b7bb9becd6486662136b58266785fc";
const ASSISTANT_ID: &str =
    "message:734d505a3d2627018421bbcde87797652eeb950d1fccb7efaf372c0b5ee7c31e";
const INPUT_DIGEST: &str =
    "sha256:4c639c4f417f7a58319b87e41b6e55339852c6840834628fda032b9cb5034be0";
const OUTCOME_DIGEST: &str =
    "sha256:9906d004a306f5a626335d60c74b0f3460146c3655132532b93162196031176d";

const INITIAL: &str = r#"{
    "request": {
        "turn_id": "turn",
        "conversation_id": "source",
        "conversation_revision": 5,
        "content": [
            {"type": "text", "text": "こんにちは\u001f"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
        ],
        "action_approval_mode": "agent",
        "chat_references": [{"kind": "chat", "profile_id": "profile", "id": "reference"}],
        "resolved_chat_references": {
            "kind": "tobkiri.chat.reference.snapshot.v1",
            "profile_id": "profile",
            "store_revision": 5,
            "project_revision": 8,
            "references": [{
                "kind": "chat",
                "id": "reference",
                "label": "参考",
                "conversation_ids": ["reference"],
                "snapshot_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "member_count": 1,
                "membership_complete": true
            }],
            "snapshot_time": 17,
            "expires_at": 600017,
            "next_cursor": null,
            "truncated": false
        },
        "task_context": {
            "version": "tobkiri.saved-task-context.v1",
            "source_id": "task",
            "conversation_id": "source",
            "recipient_id": "conversation:source",
            "input_id": "turn",
            "source_revision": 19,
            "generation": 2,
            "items": [{"id": "one", "kind": "goal", "body": "確認"}]
        },
        "context_binding": {
            "version": "tobkiri.conversation-context-binding.v1",
            "profile_id": "profile",
            "parent_conversation_id": "parent",
            "parent_revision": 9,
            "context_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        },
        "thinking_level": "xhigh",
        "strategy_reference": "s",
        "strategy_maximum_cost_microusd": 9,
        "tool_selection": {"mode": "auto", "include": [{"kind": "service", "id": "tools/read"}]}
    }
}"#;

const TRACE: &str = r#"[
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "調べる"}],
        "tool_calls": [{
            "id": "call:1",
            "type": "function",
            "function": {"name": "tools/read", "arguments": "{\"z\":1,\"a\":true}"}
        }]
    },
    {"role": "tool", "tool_call_id": "call:1", "content": "結果"}
]"#;

const LOGS: &str = r#"[
    {"tool_name": "tools/read", "tool_call_id": "call:1", "arguments": {"a": true, "z": 1}, "result": "結果"}
]"#;

fn fixture() -> (PyValue, PyValue, PyValue) {
    let initial = loads(INITIAL).unwrap();
    let request = initial.get("request").unwrap();
    let references = request
        .get("resolved_chat_references")
        .and_then(|value| value.get("references"))
        .unwrap();
    let metadata = [
        ("turn_id", text("turn")),
        ("action_approval_mode", text("agent")),
        ("chat_references", references.clone()),
    ];
    let user = object([
        ("id", text(USER_ID)),
        ("role", text("user")),
        ("status", text("complete")),
        ("parent_id", PyValue::Null),
        ("content", request.get("content").unwrap().clone()),
        ("metadata", object(metadata.clone())),
        ("tool_logs", PyValue::Null),
    ]);
    let assistant = object([
        ("id", text(ASSISTANT_ID)),
        ("role", text("assistant")),
        ("status", text("complete")),
        ("parent_id", text(USER_ID)),
        ("content", text("完了")),
        (
            "metadata",
            object(
                metadata
                    .into_iter()
                    .chain([("saved_tool_messages", loads(TRACE).unwrap())]),
            ),
        ),
        ("tool_logs", loads(LOGS).unwrap()),
    ]);
    (initial, user, assistant)
}

#[test]
fn user_and_tool_assistant_receipts_match_python_witness() {
    let (initial, user, assistant) = fixture();
    let first = append(None, &initial, "source", 5, &user).unwrap();
    let expected = object([
        ("turn_id", text("turn")),
        ("conversation_id", text("source")),
        ("input_digest", text(INPUT_DIGEST)),
        ("initial_revision", number(5)),
        ("user_message_id", text(USER_ID)),
        ("assistant_message_id", text(ASSISTANT_ID)),
        ("user_revision", number(6)),
    ]);
    assert_eq!(first, expected);
    let final_receipt = append(Some(&first), &initial, "source", 6, &assistant).unwrap();
    let mut expected = expected.into_object().unwrap();
    expected.push((
        "result_reference".into(),
        object([
            ("conversation_id", text("source")),
            ("conversation_revision", number(7)),
            ("user_message_id", text(USER_ID)),
            ("assistant_message_id", text(ASSISTANT_ID)),
            ("outcome_digest", text(OUTCOME_DIGEST)),
        ]),
    ));
    assert_eq!(final_receipt, PyValue::Object(expected));
}

#[test]
fn saved_receipts_reject_replays_rebinding_and_transcript_divergence() {
    let (initial, user, assistant) = fixture();
    let first = append(None, &initial, "source", 5, &user).unwrap();
    let changed_input = loads(&INITIAL.replace("\"xhigh\"", "\"medium\"")).unwrap();
    assert_eq!(
        append(Some(&first), &changed_input, "source", 6, &assistant),
        Err(AppendError::Invalid)
    );
    assert_eq!(
        append(Some(&first), &initial, "source", 5, &user),
        Err(AppendError::Invalid)
    );
    assert_eq!(
        append(Some(&first), &initial, "source", 7, &assistant),
        Err(AppendError::Invalid)
    );
    let mut wrong_logs = assistant.into_object().unwrap();
    wrong_logs.last_mut().unwrap().1 = loads(&LOGS.replace("結果", "別の結果")).unwrap();
    assert_eq!(
        append(
            Some(&first),
            &initial,
            "source",
            6,
            &PyValue::Object(wrong_logs)
        ),
        Err(AppendError::Invalid)
    );
}

#[test]
fn requests_reject_invalid_raster_references_and_task_scope() {
    let (_, user, _) = fixture();
    for invalid in [
        INITIAL.replace("iVBORw0KGgo=", "iVBORw0KGg=="),
        INITIAL.replace("600017", "600018"),
        INITIAL.replace("\"member_count\": 1", "\"member_count\": true"),
        INITIAL.replace("conversation:source", "conversation:other"),
        INITIAL.replace("\"parent_revision\": 9", "\"parent_revision\": 0"),
    ] {
        assert_eq!(
            append(None, &loads(&invalid).unwrap(), "source", 5, &user),
            Err(AppendError::Invalid)
        );
    }
}

#[test]
fn tool_transcript_requires_strict_arguments_and_results_in_call_order() {
    for invalid in [
        TRACE.replace(r#"\"z\":1"#, r#"\"z\":1.0"#),
        TRACE.replace(r#"\"z\":1"#, r#"\"a\":1"#),
        TRACE.replace(
            "\"tool_call_id\": \"call:1\"",
            "\"tool_call_id\": \"call:2\"",
        ),
        TRACE.replace("\"content\": \"結果\"", "\"content\": null"),
    ] {
        assert_eq!(
            trace_logs(&loads(&invalid).unwrap()),
            Err(AppendError::Invalid)
        );
    }
}
