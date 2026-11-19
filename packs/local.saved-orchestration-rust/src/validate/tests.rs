use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};

use super::{MAX_IMAGE_BYTES, MAX_TEXT_BYTES, request, saved_user_content, state_request};
use crate::Error;

fn basic_request() -> Value {
    json!({"conversation_id":"chat-1","turn_id":"turn-1","conversation_revision":1,"content":"hi"})
}

fn content_with_images(text: &str, urls: &[&str]) -> Value {
    let mut parts = vec![json!({"type":"text","text":text})];
    parts.extend(
        urls.iter()
            .map(|url| json!({"type":"image_url","image_url":{"url":url}})),
    );
    Value::Array(parts)
}

#[test]
fn raster_content_preserves_python_padding_whitespace_and_utf8_boundaries() {
    let png = "data:image/png;base64,iVBORw0KGgp=";
    let webp = "data:image/webp;base64,UklGRgAAAABXRUJQ";
    assert!(saved_user_content(&content_with_images("hi", &[png, webp])));
    assert!(!saved_user_content(&content_with_images(
        "hi",
        &[png, png, png]
    )));
    assert!(!saved_user_content(&content_with_images(
        "\u{1f}\u{85}",
        &[png]
    )));
    for url in [
        "data:image/gif;base64,iVBORw0KGgp=",
        "data:image/png;base64,iVBORw0KGgp_",
        "data:image/png;base64,iVBORw0KGgp",
        "https://images.example.com/image.png",
    ] {
        assert!(!saved_user_content(&content_with_images("hi", &[url])));
    }
    let text = "é".repeat(MAX_TEXT_BYTES / 2);
    assert!(saved_user_content(&json!(text)));
    assert!(!saved_user_content(&json!(text + "!")));
}

#[test]
fn request_budget_counts_canonical_escaping_across_image_blocks() {
    let mut raster = b"GIF89a".to_vec();
    raster.resize(MAX_IMAGE_BYTES, 0);
    let url = format!("data:image/gif;base64,{}", STANDARD.encode(&raster));
    let mut input = basic_request();
    input["content"] = content_with_images("hi", &[&url, &url]);
    assert_eq!(request(&input), Ok(()));

    let escaped_text = "\0".repeat(MAX_TEXT_BYTES);
    input["content"] = content_with_images(&format!("X{}", &escaped_text[1..]), &[&url, &url]);
    assert!(saved_user_content(&input["content"]));
    assert_eq!(request(&input), Err(Error::Capacity));
}

#[test]
fn retained_request_revalidates_options_without_requiring_content() {
    let mut input = basic_request();
    input["strategy_reference"] = Value::Null;
    input["thinking_level"] = json!("xhigh");
    input["action_approval_mode"] = json!("ask");
    input["tool_selection"] = json!({
        "mode":"auto",
        "scope":"conversation",
        "include":["tool/entity", {"kind":"service", "id":"registered:service/path"}],
        "exclude":["tool/entity"],
        "must_use":true
    });
    input["context_binding"] = json!({
        "version":"tobkiri.conversation-context-binding.v1",
        "profile_id":"profile-1",
        "parent_conversation_id":"chat-0",
        "parent_revision":1,
        "context_digest":format!("sha256:{}", "b".repeat(64))
    });
    assert_eq!(request(&input), Ok(()));
    assert!(state_request(&input).is_err());
    input.as_object_mut().unwrap().remove("content");
    assert_eq!(state_request(&input), Ok(()));
    input["strategy_maximum_cost_microusd"] = json!(1);
    assert!(state_request(&input).is_err());
    input
        .as_object_mut()
        .unwrap()
        .remove("strategy_maximum_cost_microusd");
    input["tool_selection"]["mode"] = json!("none");
    assert!(state_request(&input).is_err());
    input["tool_selection"] = json!({"mode":"none", "exclude":["tool/entity"]});
    assert_eq!(state_request(&input), Ok(()));
    input["context_binding"]["parent_revision"] = json!(true);
    assert!(state_request(&input).is_err());
}

#[test]
fn task_context_binds_turn_and_conversation_and_caps_aggregate_bytes() {
    let mut input = basic_request();
    input["task_context"] = json!({
        "version":"tobkiri.saved-task-context.v1", "source_id":"source-1",
        "conversation_id":"chat-1", "recipient_id":"conversation:chat-1", "input_id":"turn-1",
        "source_revision":0, "generation":9_007_199_254_740_991_i64,
        "items":[{"id":"item-1","kind":"todo","body":"task"}]
    });
    assert_eq!(request(&input), Ok(()));
    input["task_context"]["recipient_id"] = json!("conversation:chat-2");
    assert!(request(&input).is_err());
    input["task_context"]["recipient_id"] = json!("conversation:chat-1");
    input["task_context"]["items"] = json!([
        {"id":"item-1", "kind":"todo", "body":"a".repeat(32_000)},
        {"id":"item-2", "kind":"goal", "body":"b".repeat(32_000)}
    ]);
    assert_eq!(request(&input), Err(Error::Capacity));
}

#[test]
fn reference_snapshot_requires_complete_membership_and_the_original_order() {
    let mut input = basic_request();
    input["chat_references"] = json!([
        {"kind":"chat","profile_id":"profile-1","id":"ref-chat"},
        {"kind":"group","profile_id":"profile-1","id":"ref-group"}
    ]);
    assert!(request(&input).is_err());
    let digest = format!("sha256:{}", "a".repeat(64));
    input["resolved_chat_references"] = json!({
        "kind":"tobkiri.chat.reference.snapshot.v1", "profile_id":"profile-1",
        "store_revision":0, "project_revision":1, "snapshot_time":100, "expires_at":600_100,
        "next_cursor":null, "truncated":false,
        "references":[
            {"kind":"chat", "id":"ref-chat", "label":"Chat", "member_count":1,
             "conversation_ids":["ref-chat"], "membership_complete":true, "snapshot_digest":digest},
            {"kind":"group", "id":"ref-group", "label":"Group", "member_count":0,
             "conversation_ids":[], "membership_complete":true, "snapshot_digest":digest}
        ]
    });
    assert_eq!(request(&input), Ok(()));
    input["chat_references"].as_array_mut().unwrap().reverse();
    assert!(request(&input).is_err());
    input["chat_references"].as_array_mut().unwrap().reverse();
    input["resolved_chat_references"]["references"][0]["conversation_ids"][0] =
        json!("different-chat");
    assert!(request(&input).is_err());
    input["resolved_chat_references"]["references"][0]["conversation_ids"][0] = json!("ref-chat");
    input["resolved_chat_references"]["expires_at"] = json!(600_101);
    assert!(request(&input).is_err());
    input["resolved_chat_references"]["expires_at"] = json!(600_100);
    let members: Vec<_> = (0..256).map(|index| format!("chat-{index:0251}")).collect();
    let group = &mut input["resolved_chat_references"]["references"][1];
    group["conversation_ids"] = json!(members);
    group["member_count"] = json!(256);
    assert_eq!(request(&input), Err(Error::Capacity));
}
