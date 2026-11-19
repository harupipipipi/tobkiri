//! Replay CPython 3.13.16's captured Conversation owner and saved intents.

use super::{AppendError, append};
use serde_json::Value;

const ORACLE: &str = include_str!("../fixtures/legacy-append-oracle.json");

fn oracle() -> Value {
    serde_json::from_str(ORACLE).expect("valid Python oracle")
}

fn times(case: &Value) -> impl Iterator<Item = i64> + '_ {
    case["clock_ms"]
        .as_array()
        .expect("recorded Python clock calls")
        .iter()
        .map(|value| value.as_i64().expect("clock milliseconds"))
}

fn check_stage<'a>(profile: &str, document: &str, stage: &'a Value) -> &'a str {
    let mut clock = times(stage);
    let request = &stage["request"];
    let reply = append(profile, document, &request.to_string(), || {
        Ok(clock.next().expect("unexpected clock read"))
    })
    .expect("accepted Python append");
    assert_eq!(clock.next(), None, "all Python clock reads are required");

    let reply: Value = serde_json::from_str(&reply).expect("valid component reply");
    let owner: Value = serde_json::from_str(reply["owner_result"].as_str().unwrap()).unwrap();
    assert_eq!(owner, stage["owner_result"]);
    let actual = reply["next_document"].as_str().unwrap();
    let turn_id = request["saved_input"]["request"]["turn_id"]
        .as_str()
        .unwrap();
    let state: Value = serde_json::from_str(actual).unwrap();
    assert_eq!(state["saved_receipts"][turn_id], stage["receipt"]);
    let expected = stage["next_document"].as_str().unwrap();
    assert_eq!(actual, expected);
    expected
}

fn check_rejection(profile: &str, document: &str, case: &Value, expected: AppendError) {
    let name = case["name"].as_str().unwrap();
    let mut clock = times(case);
    let error = append(profile, document, &case["request"].to_string(), || {
        Ok(clock
            .next()
            .unwrap_or_else(|| panic!("{name}: unexpected clock read")))
    });
    assert_eq!(
        error,
        Err(expected),
        "{name} returned {} in Python: {}",
        case["python_exception"],
        case["python_message"]
    );
}

#[test]
fn ordinary_saved_appends_match_python_message_receipt_and_document_bytes() {
    let sample = oracle();
    let profile = sample["profile"].as_str().unwrap();
    let before_user = sample["document"].as_str().unwrap();
    let before_assistant = check_stage(profile, before_user, &sample["user"]);
    check_stage(profile, before_assistant, &sample["assistant"]);
}

#[test]
fn saved_append_rejects_stale_revision_and_invalid_parents() {
    let sample = oracle();
    let profile = sample["profile"].as_str().unwrap();
    let document = sample["user"]["next_document"].as_str().unwrap();
    for rejected in sample["rejected_after_user"].as_array().unwrap() {
        let expected = match rejected["name"].as_str().unwrap() {
            "stale_revision" => AppendError::Conflict,
            "unknown_parent" => AppendError::NotFound,
            "wrong_existing_parent" | "unlinked_context_binding" => AppendError::Invalid,
            name => panic!("unexpected case {name}"),
        };
        check_rejection(profile, document, rejected, expected);
    }
}

#[test]
fn linked_saved_appends_match_python_and_revalidate_parent_context() {
    let sample = oracle();
    let profile = sample["profile"].as_str().unwrap();
    let linked = &sample["linked"];
    let before_user = linked["document"].as_str().unwrap();
    let before_assistant = check_stage(profile, before_user, &linked["user"]);
    check_stage(profile, before_assistant, &linked["assistant"]);
    check_rejection(
        profile,
        linked["stale_parent_document"].as_str().unwrap(),
        &linked["stale_parent"],
        AppendError::Invalid,
    );
}
