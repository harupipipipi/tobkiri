use super::*;
use serde_json::{Value, json};

const SOURCE_DIGEST: &str =
    "sha256:5d1376d3d8f16674eef344609bd77b9a162cf20b6fc3c06add6d4a272351d52c";
const ACCEPTED_DIGEST: &str =
    "sha256:8a8e9fe1ee84eba0783d847b7cc30b170a8d45c255758796a4aa90a7c87911f5";
const REFERENCE_DIGEST: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn source() -> Value {
    json!({"request": {
        "turn_id": "turn-1",
        "conversation_id": "conv-1",
        "conversation_revision": 42,
        "content": [
            {"type": "text", "text": "選択 🧪\n\"quoted\"\u{1}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
        ],
        "tool_selection": {
            "mode": "manual",
            "scope": "conversation",
            "must_use": false,
            "include": [{"id": "local.service/foo", "kind": "service"}]
        },
        "strategy_reference": null,
        "chat_references": [{"kind": "chat", "profile_id": "profile", "id": "chat-1"}]
    }})
}

fn task_context() -> Value {
    json!({
        "version": "tobkiri.saved-task-context.v1",
        "source_id": "source-1",
        "conversation_id": "conv-1",
        "recipient_id": "conversation:conv-1",
        "input_id": "turn-1",
        "source_revision": 3,
        "generation": 8,
        "items": [{"id": "first", "kind": "todo", "body": "確認する"}]
    })
}

fn reference_snapshot() -> Value {
    json!({
        "kind": "tobkiri.chat.reference.snapshot.v1",
        "profile_id": "profile",
        "store_revision": 2,
        "project_revision": 0,
        "snapshot_time": 100,
        "expires_at": 600100,
        "next_cursor": null,
        "truncated": false,
        "references": [{
            "kind": "chat",
            "id": "chat-1",
            "label": "別の会話",
            "conversation_ids": ["chat-1"],
            "snapshot_digest": REFERENCE_DIGEST,
            "member_count": 1,
            "membership_complete": true
        }]
    })
}

fn accepted() -> Value {
    let mut accepted = source();
    accepted["request"]["task_context"] = task_context();
    accepted["request"]["resolved_chat_references"] = reference_snapshot();
    accepted
}

fn bytes(value: &Value) -> Vec<u8> {
    serde_json::to_vec(value).unwrap()
}

#[test]
fn captures_python_digests_and_projection_bytes() {
    let source = serde_json::to_vec_pretty(&source()).unwrap();
    let accepted = accepted();
    let capture = prepare_saved_input(&source, &bytes(&accepted)).unwrap();

    // Produced by the Python validator, canonical_digest and original writer.
    assert_eq!(capture.turn_id, "turn-1");
    assert_eq!(capture.source_digest, SOURCE_DIGEST);
    assert_eq!(capture.accepted_digest, ACCEPTED_DIGEST);
    let expected_context = format!(
        r#"{{"kind":"tobkiri.saved.input-projection.v1","task_context":{},"resolved_chat_references":{},"target_settings":{{"tool_selection":{}}}}}"#,
        task_context(),
        reference_snapshot(),
        accepted["request"]["tool_selection"],
    );
    assert_eq!(capture.context, expected_context.as_bytes());
    assert_eq!(capture.accepted, bytes(&accepted));
    assert_eq!(
        saved_input_source(&source).unwrap(),
        SavedInputSource {
            turn_id: "turn-1".into(),
            source_digest: SOURCE_DIGEST.into(),
        },
    );
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, ACCEPTED_DIGEST, &capture.context).unwrap(),
        capture.accepted,
    );
}

#[test]
fn replays_old_projection_and_task_context_only_rows() {
    let source = source();
    let accepted = accepted();
    let context = json!({
        "kind": PROJECTION_KIND,
        "task_context": task_context(),
        "resolved_chat_references": reference_snapshot(),
    });
    assert_eq!(
        replay_saved_input(
            &bytes(&source),
            SOURCE_DIGEST,
            ACCEPTED_DIGEST,
            &bytes(&context),
        )
        .unwrap(),
        bytes(&accepted),
    );

    let mut contextual_source = source.clone();
    contextual_source["request"]["task_context"] = task_context();
    let contextual_source = bytes(&contextual_source);
    let digest = saved_input_source(&contextual_source)
        .unwrap()
        .source_digest;
    assert_eq!(
        replay_saved_input(
            &contextual_source,
            &digest,
            &digest,
            &bytes(&task_context()),
        )
        .unwrap(),
        contextual_source,
    );
    assert_eq!(
        replay_saved_input(&contextual_source, &digest, SOURCE_DIGEST, b"null").unwrap(),
        bytes(&source),
    );
}

#[test]
fn captures_only_context_and_allows_owner_to_replace_source_task_context() {
    let mut source = source();
    source["request"]["task_context"] = task_context();
    let mut accepted = accepted();
    accepted["request"]["task_context"]["generation"] = json!(9);
    let capture = prepare_saved_input(&bytes(&source), &bytes(&accepted)).unwrap();
    assert_eq!(
        replay_saved_input(
            &bytes(&source),
            &capture.source_digest,
            &capture.accepted_digest,
            &capture.context,
        )
        .unwrap(),
        bytes(&accepted),
    );

    for (field, replacement) in [
        ("content", json!("different content")),
        ("turn_id", json!("turn-2")),
        ("conversation_id", json!("conv-2")),
        ("conversation_revision", json!(43)),
        ("tool_selection", json!({"mode": "none"})),
        ("strategy_reference", json!("another-strategy")),
    ] {
        let mut changed = source.clone();
        changed["request"]
            .as_object_mut()
            .unwrap()
            .remove("task_context");
        changed["request"][field] = replacement;
        assert_eq!(
            prepare_saved_input(&bytes(&source), &bytes(&changed)),
            Err(Error::Conflict),
            "{field}",
        );
    }
    assert_eq!(
        prepare_saved_input(&bytes(&accepted), &bytes(&accepted)),
        Err(Error::Conflict),
    );
}

#[test]
fn validates_capture_replay_identities_and_limits() {
    let source = bytes(&source());
    let capture = prepare_saved_input(&source, &bytes(&accepted())).unwrap();
    assert_eq!(
        replay_saved_input(&source, ACCEPTED_DIGEST, ACCEPTED_DIGEST, &capture.context),
        Err(Error::Conflict),
    );
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, SOURCE_DIGEST, &capture.context),
        Err(Error::InvalidRecord),
    );
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, "invalid digest", &capture.context),
        Err(Error::InvalidRecord),
    );
    let mut context: Value = serde_json::from_slice(&capture.context).unwrap();
    context["target_settings"]["tool_selection"]["mode"] = json!("auto");
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, ACCEPTED_DIGEST, &bytes(&context)),
        Err(Error::InvalidRecord),
    );
    context["target_settings"]["tool_selection"]["mode"] = json!("manual");
    context["target_settings"]["tool_selection"]["must_use"] = json!(0.0);
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, ACCEPTED_DIGEST, &bytes(&context)).unwrap(),
        capture.accepted,
    );
    context["unknown_field"] = json!(null);
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, ACCEPTED_DIGEST, &bytes(&context)),
        Err(Error::InvalidRecord),
    );
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, ACCEPTED_DIGEST, b"{"),
        Err(Error::InvalidRecord),
    );
    assert_eq!(
        replay_saved_input(
            &source,
            SOURCE_DIGEST,
            ACCEPTED_DIGEST,
            br#"{"kind":"obsolete","kind":"tobkiri.saved.input-projection.v1","task_context":null,"resolved_chat_references":null}"#,
        ),
        Err(Error::RequiresLegacyCodec),
    );
    let mut oversized = vec![b' '; RECORD_LIMIT];
    oversized[..4].copy_from_slice(b"null");
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, SOURCE_DIGEST, &oversized).unwrap(),
        source,
    );
    oversized.push(b' ');
    assert_eq!(
        replay_saved_input(&source, SOURCE_DIGEST, SOURCE_DIGEST, &oversized),
        Err(Error::Capacity),
    );
}

#[test]
fn rejects_invalid_user_content_and_owner_projection_before_writing() {
    for (field, replacement) in [
        ("content", json!("\u{1f}")),
        (
            "content",
            json!([{"type": "text", "text": "hi"}, {
                "type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}
            }]),
        ),
        ("conversation_revision", json!(9_007_199_254_740_992_u64)),
        ("conversation_revision", json!(true)),
        ("strategy_maximum_cost_microusd", json!(1.0)),
        ("tool_selection", json!({"mode": "none", "must_use": true})),
    ] {
        let mut request = source();
        request["request"][field] = replacement;
        assert_eq!(
            saved_input_source(&bytes(&request)),
            Err(Error::InvalidRequest),
            "{field}",
        );
    }

    let mut accepted = accepted();
    accepted["request"]["resolved_chat_references"]["references"][0]["membership_complete"] =
        json!(false);
    assert_eq!(
        prepare_saved_input(&bytes(&source()), &bytes(&accepted)),
        Err(Error::InvalidRequest),
    );
    accepted["request"]["resolved_chat_references"] = reference_snapshot();
    accepted["request"]["task_context"]["recipient_id"] = json!("conversation:other");
    assert_eq!(
        prepare_saved_input(&bytes(&source()), &bytes(&accepted)),
        Err(Error::InvalidRequest),
    );
}

fn prepare_capture_turn(body: &[u8]) -> Result<Option<(i64, Vec<u8>)>> {
    prepare_saved_input_capture_turn(
        "profile",
        "turn-1",
        "request-1",
        body,
        &Digest::parse(SOURCE_DIGEST).unwrap(),
        &Digest::parse(ACCEPTED_DIGEST).unwrap(),
    )
}

#[test]
fn rebinds_queued_delivery_and_calendar_with_python_owner_bytes() {
    let original = r#"{
  "id": "turn-1", "profile_id": "profile", "input_digest": "@digest",
  "request_id": "request-1", "status": "queued", "conversation_id": "conv-1",
  "updated_at": 123, "revision": 8, "created_at": 120, "conversation_revision": 42,
  "guidance": [{"value": {"text": "\u78ba\u8a8d\n\u001f", "z": -0.0, "a": 1E-7},
                "status": "queued", "id": "guidance-1"}],
  "events": [{"name": "turn.guidance_queued", "sequence": 0, "at": 123}],
  @admission,
  "extra": {"integer": 123456789012345678901234567890, "labels": ["🗓️", "a\/b"]}
}"#;
    // Golden rows from the SQL update in DurableTurnRuntime.bind_saved_input
    // for source() and accepted(), with a previously indexed timestamp of 777.
    let expected = concat!(
        r#"{"id":"turn-1","profile_id":"profile","input_digest":""#,
        "sha256:8a8e9fe1ee84eba0783d847b7cc30b170a8d45c255758796a4aa90a7c87911f5",
        r#"","request_id":"request-1","status":"queued","conversation_id":"conv-1","#,
        r#""updated_at":123,"revision":8,"created_at":120,"conversation_revision":42,"#,
        r#""guidance":[{"value":{"text":"確認\n\u001f","z":-0.0,"a":1e-07},"status":"queued","id":"guidance-1"}],"#,
        r#""events":[{"name":"turn.guidance_queued","sequence":0,"at":123}],@admission,"#,
        r#""extra":{"integer":123456789012345678901234567890,"labels":["🗓️","a/b"]}}"#,
    );
    for (admission, compact_admission) in [
        (
            r#""delivery_provenance": {"delivery_id": "delivery-1"}"#,
            r#""delivery_provenance":{"delivery_id":"delivery-1"}"#,
        ),
        (
            r#""delivery_provenance": [], "calendar_preparation": {"occurrence_id": "calendar-1"}"#,
            r#""delivery_provenance":[],"calendar_preparation":{"occurrence_id":"calendar-1"}"#,
        ),
    ] {
        let original = original
            .replace("@digest", SOURCE_DIGEST)
            .replace("@admission", admission);
        let expected = expected.replace("@admission", compact_admission);
        let (timestamp, body) = prepare_capture_turn(original.as_bytes()).unwrap().unwrap();
        assert_eq!(timestamp, 123);
        assert_eq!(body, expected.as_bytes());
        assert_eq!(prepare_capture_turn(&body), Ok(None));
    }
}

#[test]
fn rejects_conflicting_or_invalid_turns_and_leaves_accepted_rows_alone() {
    let mut row = json!({
        "profile_id": "profile",
        "id": "turn-1",
        "request_id": "request-1",
        "conversation_id": "conv-1",
        "revision": 1,
        "conversation_revision": 42,
        "created_at": 123,
        "updated_at": 123,
        "status": "queued",
        "guidance": [],
        "events": [],
        "input_digest": SOURCE_DIGEST
    });
    let prepare = |row: &Value| prepare_capture_turn(&bytes(row));
    assert_eq!(prepare(&row), Err(Error::Conflict));
    for false_value in [
        json!(null),
        json!({}),
        json!([]),
        json!(0),
        json!(""),
        json!(false),
    ] {
        row["delivery_provenance"] = false_value;
        assert_eq!(prepare(&row), Err(Error::Conflict));
    }
    row["delivery_provenance"] = json!("delivery-1");
    assert!(prepare(&row).unwrap().is_some());
    row["status"] = json!("running");
    assert_eq!(prepare(&row), Err(Error::Conflict));
    row["input_digest"] = json!(ACCEPTED_DIGEST);
    assert_eq!(prepare(&row), Ok(None));
    row["status"] = json!("queued");
    row["input_digest"] = json!(REFERENCE_DIGEST);
    assert_eq!(prepare(&row), Err(Error::Conflict));
    row["input_digest"] = json!(SOURCE_DIGEST);
    row["updated_at"] = json!(9_223_372_036_854_775_808_u64);
    assert_eq!(prepare(&row), Err(Error::InvalidRecord));
    row["updated_at"] = json!(123);
    row["saved_workflow"] = json!({
        "profile_id": "profile",
        "turn_id": "turn-1",
        "conversation_id": "conv-1",
        "input_digest": SOURCE_DIGEST,
        "run_id": "run-1",
        "definition_id": "definition-1",
        "revision_digest": REFERENCE_DIGEST,
        "policy_digest": REFERENCE_DIGEST,
        "plan_digest": REFERENCE_DIGEST,
        "activation_id": "activation-1",
        "activation_digest": REFERENCE_DIGEST,
        "security_epoch": 1,
        "workflow_principal_ids": ["principal"],
        "owner_principal_id": "owner",
        "owner_session_id": "session",
        "admission_api_version": "io.tobkiri.saved-workflow-admission.v1"
    });
    assert_eq!(prepare(&row), Err(Error::Conflict));
    row.as_object_mut().unwrap().remove("saved_workflow");
    row["input_digest"] = json!(ACCEPTED_DIGEST);
    for field in ["profile_id", "id", "request_id"] {
        let mut changed = row.clone();
        changed[field] = json!("other");
        assert_eq!(prepare(&changed), Err(Error::InvalidRecord));
    }
    assert_eq!(prepare_capture_turn(b"{"), Err(Error::InvalidRecord));
}
