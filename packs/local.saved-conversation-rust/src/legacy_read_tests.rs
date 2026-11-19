use super::*;
use serde_json::{Value, json};

const PROFILE: &str = "profile-1";
const DOCUMENT: &str = include_str!("../fixtures/legacy-get-document.json");
const OWNER: &str = include_str!("../fixtures/legacy-get-owner-result.json");
const LIST_DOCUMENT: &str = include_str!("../fixtures/legacy-list-document.json");
const LIST_OWNER: &str = include_str!("../fixtures/legacy-list-owner-result.json");
const RECEIPT_DOCUMENT: &str = include_str!("../fixtures/legacy-receipt-document.json");
const RECEIPT_OWNER: &str = include_str!("../fixtures/legacy-receipt-owner-result.json");

fn document(conversations: &str) -> String {
    format!(
        r#"{{"version":"{STORE_VERSION}","profile_id":"{PROFILE}","conversations":{conversations}}}"#
    )
}

fn receipt_document(receipts: &str) -> String {
    format!(
        r#"{{"version":"{STORE_VERSION}","profile_id":"{PROFILE}","conversations":{{}},"saved_receipts":{receipts}}}"#
    )
}

fn owner(reply: &str) -> String {
    let fields = loads(reply).unwrap().into_object().unwrap();
    assert_eq!(fields.len(), 1);
    assert_eq!(fields[0].0, "owner_result");
    fields[0].1.as_str().unwrap().to_owned()
}

#[test]
fn matches_the_python_owner_row_including_linked_context_and_opaque_numbers() {
    for id in ["C.1_x-y:z", "\u{001c}\u{001f}C.1_x-y:z\u{3000}\t"] {
        let reply = get(PROFILE, Some(DOCUMENT), id).unwrap();
        assert_eq!(owner(&reply), OWNER.trim_end());
    }
}

#[test]
fn list_matches_the_python_snapshot_including_legacy_rows_and_numbers() {
    let reply = list(PROFILE, Some(LIST_DOCUMENT)).unwrap();
    assert_eq!(owner(&reply), LIST_OWNER.trim_end());
}

#[test]
fn list_defaults_only_absent_document_and_migration() {
    let snapshot = |revision| {
        format!(
            r#"{{"version":"{STORE_VERSION}","profile_id":"{PROFILE}","revision":{revision},"conversations":[],"migration":null}}"#
        )
    };
    assert_eq!(owner(&list(PROFILE, None).unwrap()), snapshot("0"));

    for revision in ["-0.0", "true", "null", r#"{"z":1,"a":2}"#] {
        let raw = list_document(revision, "{}");
        assert_eq!(
            owner(&list(PROFILE, Some(&raw)).unwrap()),
            snapshot(revision)
        );
    }
    let missing_revision = document("{}");
    assert_eq!(
        list(PROFILE, Some(&missing_revision)),
        Err(ReadError::LegacyCodec)
    );
}

#[test]
fn list_checks_profile_and_document_shape_before_compatibility() {
    assert_eq!(list("../foreign", None), Err(ReadError::Profile));
    assert_eq!(
        list("foreign", Some(LIST_DOCUMENT)),
        Err(ReadError::Profile)
    );
    for raw in ["bad".to_owned(), list_document("1", "[]")] {
        assert_eq!(list(PROFILE, Some(&raw)), Err(ReadError::InvalidDocument));
    }

    for rows in [
        r#"{"x":NaN}"#,
        r#"{"x":{"\ud800":"\ud800"}}"#,
        r#"{"x":1,"x":2}"#,
        &format!(r#"{{"x":{}0{}}}"#, "[".repeat(256), "]".repeat(256)),
    ] {
        let raw = list_document("1", rows);
        assert_eq!(list(PROFILE, Some(&raw)), Err(ReadError::LegacyCodec));
        assert_eq!(list("foreign", Some(&raw)), Err(ReadError::Profile));
    }
}

#[test]
fn list_respects_both_document_and_escaped_owner_result_limits() {
    let raw = list_document("1", &format!(r#"{{"x":"{}"}}"#, "z".repeat(MAX_BYTES)));
    assert_eq!(list(PROFILE, Some(&raw)), Err(ReadError::Limits));

    let raw = list_document("1", &format!(r#"{{"x":"{}"}}"#, r#"\""#.repeat(300_000)));
    assert!(raw.len() < MAX_BYTES);
    assert_eq!(list(PROFILE, Some(&raw)), Err(ReadError::LegacyCodec));
}

fn list_document(revision: &str, conversations: &str) -> String {
    format!(
        r#"{{"version":"{STORE_VERSION}","profile_id":"{PROFILE}","revision":{revision},"conversations":{conversations}}}"#
    )
}

fn linked_rows() -> Value {
    json!({
        "child": {
            "id": "child", "conversation_revision": 2, "parent_conversation_id": "parent",
            "context_link": {
                "version": "tobkiri.conversation-context-link.v1",
                "parent_conversation_id": "parent", "slot": "side-chat",
                "created_from_parent_revision": 3,
            },
            "metadata": {"icon_svg": "old", "conversation_channel": "side"},
            "messages": [{"id": "user-1", "sequence": 99}],
        },
        "parent": {
            "id": "parent", "conversation_revision": 7, "model_reference": "local-model",
            "metadata": {"thinking_level": "medium", "title": "private", "icon_svg": "old"},
            "messages": [{"id": "parent-message", "content": "Parent history"}],
        },
        "decoy": {"id": "decoy", "model_reference": "other-model"},
    })
}

#[test]
fn linked_read_preserves_the_child_and_projects_only_inherited_parent_fields() {
    let raw = document(&linked_rows().to_string());
    let candidate = loads(&get_linked(PROFILE, Some(&raw), " child ").unwrap()).unwrap();
    assert_eq!(
        candidate.get("parent_read").and_then(PyValue::as_str),
        Some(owner(&get(PROFILE, Some(&raw), "parent").unwrap()).as_str()),
    );
    let paired = candidate.get("owner_result").unwrap().as_str().unwrap();
    let single = owner(&get(PROFILE, Some(&raw), "child").unwrap());
    let paired: Value = serde_json::from_str(paired).unwrap();
    let single: Value = serde_json::from_str(&single).unwrap();
    assert_eq!(paired["conversation"], single["conversation"]);
    assert_eq!(
        paired["parent_context"],
        json!({
            "id": "parent", "conversation_revision": 7, "model_reference": "local-model",
            "system_prompt_id": null, "agent_id": null, "conversation_kind": null,
            "group_id": null, "tags": null, "metadata": {"thinking_level": "medium"},
        })
    );
}

#[test]
fn linked_read_does_not_follow_an_old_unlinked_parent_id() {
    for explicit_null in [false, true] {
        let mut rows = linked_rows();
        if explicit_null {
            rows["child"]["context_link"] = Value::Null;
        } else {
            rows["child"]
                .as_object_mut()
                .unwrap()
                .remove("context_link");
        }
        let raw = document(&rows.to_string());
        let result = owner(&get_linked(PROFILE, Some(&raw), "child").unwrap());
        let result: Value = serde_json::from_str(&result).unwrap();
        assert!(result["parent_context"].is_null());
        assert_eq!(result["conversation"]["parent_conversation_id"], "parent");
    }
}

#[test]
fn linked_read_rejects_invalid_missing_self_or_nested_links() {
    let check = |rows: Value, expected| {
        let raw = document(&rows.to_string());
        assert_eq!(get_linked(PROFILE, Some(&raw), "child"), Err(expected));
        // Strict link inspection belongs to this private read. Normal GET can
        // still return the old row for diagnostics and repair.
        assert!(get(PROFILE, Some(&raw), "child").is_ok());
    };
    let mut rows = linked_rows();
    rows["child"]["context_link"]["created_from_parent_revision"] = json!(true);
    check(rows, ReadError::InvalidDocument);
    let mut rows = linked_rows();
    rows["child"]["context_link"]["parent_conversation_id"] = json!("decoy");
    check(rows, ReadError::InvalidDocument);
    let mut rows = linked_rows();
    rows["parent"] = json!([]);
    check(rows, ReadError::NotFound);
    let mut rows = linked_rows();
    rows["parent"]["id"] = json!("decoy");
    check(rows, ReadError::InvalidDocument);
    let mut rows = linked_rows();
    rows["child"]["parent_conversation_id"] = json!("child");
    rows["child"]["context_link"]["parent_conversation_id"] = json!("child");
    check(rows, ReadError::InvalidDocument);
    let mut rows = linked_rows();
    rows["parent"]["parent_conversation_id"] = json!("decoy");
    rows["parent"]["context_link"] = rows["child"]["context_link"].clone();
    rows["parent"]["context_link"]["parent_conversation_id"] = json!("decoy");
    check(rows, ReadError::InvalidDocument);
}

#[test]
fn linked_read_checks_profile_and_both_reply_limits() {
    let mut rows = linked_rows();
    rows["child"]["content"] = json!("\"".repeat(MAX_BYTES / 4));
    let raw = document(&rows.to_string());
    assert!(raw.len() < MAX_BYTES);
    assert_eq!(
        get_linked("foreign", Some(&raw), "child"),
        Err(ReadError::Profile)
    );
    assert_eq!(
        get_linked(PROFILE, Some(&raw), "child"),
        Err(ReadError::LegacyCodec)
    );

    let mut rows = linked_rows();
    rows["parent"]["content"] = json!("\"".repeat(MAX_BYTES / 4));
    let raw = document(&rows.to_string());
    assert!(raw.len() < MAX_BYTES);
    assert_eq!(
        get(PROFILE, Some(&raw), "parent"),
        Err(ReadError::LegacyCodec)
    );
    let reply = get_linked(PROFILE, Some(&raw), "child").unwrap();
    assert!(loads(&reply).unwrap().get("parent_read").is_none());
    let projection: Value = serde_json::from_str(&owner(&reply)).unwrap();
    assert_eq!(projection["parent_context"]["id"], "parent");
}

#[test]
fn normalizes_metadata_and_only_normalizes_lists_of_mappings() {
    for (id, expected) in [
        ("absent-meta", r#"{"conversation":{"metadata":{}}}"#),
        (
            "mixed-messages",
            r#"{"conversation":{"metadata":{},"messages":[{"sequence":99},"legacy"]}}"#,
        ),
        (
            "non-array",
            r#"{"conversation":{"metadata":{},"messages":{"sequence":99}}}"#,
        ),
        (
            "empty-messages",
            r#"{"conversation":{"metadata":{},"messages":[]}}"#,
        ),
    ] {
        assert_eq!(owner(&get(PROFILE, Some(DOCUMENT), id).unwrap()), expected);
    }
}

#[test]
fn preserves_host_then_document_then_identifier_precedence() {
    for (raw, id, expected) in [
        (Some("invalid"), "", ReadError::InvalidIdentifier),
        (Some(""), "a", ReadError::InvalidDocument),
        (Some("{}"), "-id", ReadError::InvalidDocument),
        (None, "-id", ReadError::InvalidIdentifier),
        (None, "a", ReadError::NotFound),
        (
            Some(DOCUMENT),
            "\u{001c}\u{0085}",
            ReadError::InvalidIdentifier,
        ),
        (Some(DOCUMENT), "é", ReadError::InvalidIdentifier),
        (
            Some(DOCUMENT),
            "C.1_x-y:z\u{200b}",
            ReadError::InvalidIdentifier,
        ),
        (Some(DOCUMENT), "c.1_x-y:z", ReadError::NotFound),
        (Some(DOCUMENT), "other", ReadError::NotFound),
    ] {
        assert_eq!(get(PROFILE, raw, id), Err(expected), "{id:?}");
    }
    assert_eq!(
        get(PROFILE, None, &"x".repeat(256)),
        Err(ReadError::NotFound)
    );
    assert_eq!(
        get(PROFILE, None, &"x".repeat(257)),
        Err(ReadError::InvalidIdentifier)
    );
    assert_eq!(
        get("profile-2", Some(DOCUMENT), "-id"),
        Err(ReadError::Profile)
    );
    for profile in ["", " profile-1", "path/profile-1", "..", "a\0"] {
        assert_eq!(get(profile, None, "a"), Err(ReadError::Profile));
    }
}

#[test]
fn only_valid_cpython_only_documents_for_this_profile_request_fallback() {
    for conversations in [
        r#"{"a":{"value":1,"value":2}}"#,
        r#"{"a":{"value":NaN,"other":Infinity}}"#,
        r#"{"a":{"value":1e999}}"#,
        r#"{"a":{"\ud800":"\ud800"}}"#,
    ] {
        let raw = document(conversations);
        assert_eq!(get(PROFILE, Some(&raw), "a"), Err(ReadError::LegacyCodec));
        assert_eq!(get("foreign", Some(&raw), "a"), Err(ReadError::Profile));
        assert_eq!(
            get(PROFILE, Some(&raw), "-a"),
            Err(ReadError::InvalidIdentifier)
        );
        let malformed = format!("{raw}x");
        assert_eq!(
            get(PROFILE, Some(&malformed), "a"),
            Err(ReadError::InvalidDocument)
        );
    }

    for raw in [
        r#"{"version":"rumi.conversation-store.v1","profile_id":"profile-1","conversations":NaN}"#,
        r#"{"version":"rumi.conversation-store.v1","profile_id":"profile-1","conversations":[],"extra":NaN}"#,
        r#"{"version":"other","profile_id":"profile-1","conversations":{},"extra":NaN}"#,
        r#"{"version":"rumi.conversation-store.v1","conversations":{},"extra":NaN}"#,
        r#"{"version":"rumi.conversation-store.v1","profile_id":null,"conversations":{},"extra":NaN}"#,
    ] {
        assert_eq!(
            get(PROFILE, Some(raw), "a"),
            Err(ReadError::InvalidDocument)
        );
    }
    let overwritten_owner = r#"{"version":"rumi.conversation-store.v1","profile_id":"profile-1","profile_id":"foreign","conversations":{}}"#;
    assert_eq!(
        get(PROFILE, Some(overwritten_owner), "a"),
        Err(ReadError::Profile)
    );
    let overwritten_map = r#"{"version":"rumi.conversation-store.v1","profile_id":"profile-1","conversations":{},"conversations":[]}"#;
    assert_eq!(
        get(PROFILE, Some(overwritten_map), "a"),
        Err(ReadError::InvalidDocument)
    );
    let overwide_integer = document(&format!(r#"{{"a":{{"value":{}}}}}"#, "9".repeat(4301)));
    assert_eq!(
        get(PROFILE, Some(&overwide_integer), "a"),
        Err(ReadError::InvalidDocument)
    );
}

#[test]
fn bounds_the_document_and_the_aggregate_escaped_reply() {
    let mut at_input_limit = document(r#"{"a":{}}"#);
    at_input_limit.push_str(&" ".repeat(MAX_BYTES - at_input_limit.len()));
    assert!(get(PROFILE, Some(&at_input_limit), "a").is_ok());
    at_input_limit.push(' ');
    assert_eq!(
        get(PROFILE, Some(&at_input_limit), "a"),
        Err(ReadError::Limits)
    );

    let row = |growth| {
        // Each quote requires two bytes in the file and four in the aggregate reply.
        let quotes = r#"\""#.repeat(growth / 4);
        let remainder = "x".repeat(growth % 4);
        document(&format!(r#"{{"a":{{"value":"{quotes}{remainder}"}}}}"#))
    };
    let baseline = get(PROFILE, Some(&row(0)), "a").unwrap().len();
    let at_output_limit = row(MAX_BYTES - baseline);
    assert_eq!(
        get(PROFILE, Some(&at_output_limit), "a").unwrap().len(),
        MAX_BYTES
    );
    assert_eq!(
        get(PROFILE, Some(&row(MAX_BYTES - baseline + 1)), "a"),
        Err(ReadError::LegacyCodec)
    );
    assert!(at_output_limit.len() < MAX_BYTES);
}

#[test]
fn a_node_limit_requests_fallback_only_after_full_legacy_validation() {
    let raw = document(&format!(
        r#"{{"a":{{"values":[{}null]}}}}"#,
        "0,".repeat(100_000)
    ));
    assert_eq!(get(PROFILE, Some(&raw), "a"), Err(ReadError::LegacyCodec));
    assert_eq!(get("foreign", Some(&raw), "a"), Err(ReadError::Profile));
    let raw = format!("{raw}!");
    assert_eq!(
        get(PROFILE, Some(&raw), "a"),
        Err(ReadError::InvalidDocument)
    );
}

#[test]
fn uses_legacy_recursion_behavior_for_deep_sibling_metadata() {
    let nested = |depth| {
        let opens = "[".repeat(depth);
        let closes = "]".repeat(depth);
        document(&format!(
            r#"{{"a":{{}},"sibling":{{"metadata":{{"value":{opens}0{closes}}}}}}}"#
        ))
    };
    assert!(get(PROFILE, Some(&nested(128)), "a").is_ok());
    let deep = nested(1_000);
    assert_eq!(get(PROFILE, Some(&deep), "a"), Err(ReadError::LegacyCodec));
    assert_eq!(get("foreign", Some(&deep), "a"), Err(ReadError::Profile));
}

#[test]
fn saved_receipt_matches_the_python_owner_without_projecting_fields() {
    for id in ["Turn.1_x-y:z", "\u{001c}Turn.1_x-y:z\u{3000}\t"] {
        let reply = saved_receipt(PROFILE, Some(RECEIPT_DOCUMENT), id).unwrap();
        assert_eq!(owner(&reply), RECEIPT_OWNER.trim_end());
    }
    for (id, expected) in [
        (
            "user-only",
            r#"{"receipt":{"turn_id":"user-only","user_revision":2}}"#,
        ),
        ("empty", r#"{"receipt":{}}"#),
    ] {
        let reply = saved_receipt(PROFILE, Some(RECEIPT_DOCUMENT), id).unwrap();
        assert_eq!(owner(&reply), expected);
    }
}

#[test]
fn saved_receipt_returns_null_for_only_absence_or_non_mapping_rows() {
    for id in [
        "missing",
        "turn.1_x-y:z",
        "null",
        "integer",
        "array",
        "boolean",
        "text",
    ] {
        let reply = saved_receipt(PROFILE, Some(RECEIPT_DOCUMENT), id).unwrap();
        assert_eq!(owner(&reply), r#"{"receipt":null}"#);
    }
    for raw in [None, Some(document("{}")), Some(receipt_document("{}"))] {
        let reply = saved_receipt(PROFILE, raw.as_deref(), "missing").unwrap();
        assert_eq!(owner(&reply), r#"{"receipt":null}"#);
    }
}

#[test]
fn saved_receipt_reads_the_document_and_inventory_before_validating_the_id() {
    for (raw, id, expected) in [
        (Some("invalid"), "", ReadError::InvalidDocument),
        (Some("{}"), "-id", ReadError::InvalidDocument),
        (None, "", ReadError::InvalidIdentifier),
        (Some(RECEIPT_DOCUMENT), "-id", ReadError::InvalidIdentifier),
        (Some(RECEIPT_DOCUMENT), "é", ReadError::InvalidIdentifier),
        (
            Some(RECEIPT_DOCUMENT),
            "user-only\u{200b}",
            ReadError::InvalidIdentifier,
        ),
    ] {
        assert_eq!(saved_receipt(PROFILE, raw, id), Err(expected), "{id:?}");
    }
    for inventory in ["null", "true", "0", "[]", r#""{}""#] {
        let raw = receipt_document(inventory);
        for id in ["", "-id", "valid"] {
            assert_eq!(
                saved_receipt(PROFILE, Some(&raw), id),
                Err(ReadError::LegacyReceiptInventory)
            );
        }
        assert_eq!(
            saved_receipt("foreign", Some(&raw), ""),
            Err(ReadError::Profile)
        );
    }
    assert!(saved_receipt(PROFILE, None, &"x".repeat(256)).is_ok());
    assert_eq!(
        saved_receipt(PROFILE, None, &"x".repeat(257)),
        Err(ReadError::InvalidIdentifier)
    );
}

#[test]
fn saved_receipt_only_defers_python_compatible_documents_or_limits() {
    for inventory in [
        r#"{"id":{"value":NaN}}"#,
        r#"{"id":{"\ud800":"\ud800"}}"#,
        r#"{"id":{"value":1,"value":2}}"#,
    ] {
        let raw = receipt_document(inventory);
        assert_eq!(
            saved_receipt(PROFILE, Some(&raw), "id"),
            Err(ReadError::LegacyCodec)
        );
        assert_eq!(
            saved_receipt("foreign", Some(&raw), "id"),
            Err(ReadError::Profile)
        );
        assert_eq!(
            saved_receipt(PROFILE, Some(&format!("{raw}!")), ""),
            Err(ReadError::InvalidDocument)
        );
    }
    let wrong_owner_shape = document("[]");
    assert_eq!(
        saved_receipt(PROFILE, Some(&wrong_owner_shape), "id"),
        Err(ReadError::InvalidDocument)
    );
    let large_row = receipt_document(&format!(
        r#"{{"id":{{"value":"{}"}}}}"#,
        r#"\""#.repeat(300_000)
    ));
    assert!(large_row.len() < MAX_BYTES);
    assert_eq!(
        saved_receipt(PROFILE, Some(&large_row), "id"),
        Err(ReadError::LegacyCodec)
    );
}
