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

#[test]
fn protects_existing_turn_binding_and_defers_legacy_queued_rebinding() {
    let source_digest = Digest::parse(SOURCE_DIGEST).unwrap();
    let accepted_digest = Digest::parse(ACCEPTED_DIGEST).unwrap();
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
        "input_digest": ACCEPTED_DIGEST
    });
    let validate = |row: &Value| {
        validate_saved_input_capture_turn(
            "profile",
            "turn-1",
            "request-1",
            &bytes(row),
            &source_digest,
            &accepted_digest,
        )
    };
    assert_eq!(validate(&row), Ok(()));
    row["input_digest"] = json!(SOURCE_DIGEST);
    assert_eq!(validate(&row), Err(Error::Conflict));
    row["delivery_provenance"] = json!({});
    assert_eq!(validate(&row), Err(Error::Conflict));
    row["calendar_preparation"] = json!({"id": "prep-1"});
    assert_eq!(validate(&row), Err(Error::RequiresLegacyCodec));
    row["calendar_preparation"] = json!(null);
    row["delivery_provenance"] = json!({"id": "delivery-1"});
    assert_eq!(validate(&row), Err(Error::RequiresLegacyCodec));
    row["status"] = json!("running");
    assert_eq!(validate(&row), Err(Error::Conflict));
    row["status"] = json!("queued");
    row["input_digest"] = json!(REFERENCE_DIGEST);
    assert_eq!(validate(&row), Err(Error::Conflict));
    row["input_digest"] = json!(ACCEPTED_DIGEST);
    row["profile_id"] = json!("other");
    assert_eq!(validate(&row), Err(Error::InvalidRecord));
    assert_eq!(
        validate_saved_input_capture_turn(
            "profile",
            "turn-1",
            "request-1",
            b"{",
            &source_digest,
            &accepted_digest,
        ),
        Err(Error::InvalidRecord),
    );
}
