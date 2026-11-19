use super::*;

const PROFILE: &str = "profile-1";
const DOCUMENT: &str = include_str!("../fixtures/legacy-get-document.json");
const OWNER: &str = include_str!("../fixtures/legacy-get-owner-result.json");

fn document(conversations: &str) -> String {
    format!(
        r#"{{"version":"{STORE_VERSION}","profile_id":"{PROFILE}","conversations":{conversations}}}"#
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
