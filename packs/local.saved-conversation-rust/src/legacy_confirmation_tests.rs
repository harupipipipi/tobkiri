//! Replay Python 3.13's actual Conversation and SQLite-backed Turn owners.

use super::*;
use serde_json::{Value, json};

const ORACLE: &str = include_str!("../fixtures/legacy-confirmation-oracle.json");

fn oracle() -> Value {
    serde_json::from_str(ORACLE).expect("valid Python oracle")
}

fn evaluate(document: &str, turn: &Value) -> Result<Value, ConfirmationError> {
    confirm("defaults", document, &turn.to_string())
        .map(|result| serde_json::from_str(&result).expect("valid component reply"))
}

fn owner_result(reply: &Value) -> Value {
    serde_json::from_str(reply["owner_result"].as_str().expect("owner reply"))
        .expect("valid owner reply")
}

#[test]
fn legacy_confirmation_matches_python_bytes_and_no_op_replay() {
    let sample = oracle();
    let turn = &sample["turn"];
    let old = sample["document"].as_str().unwrap();
    let expected = sample["promotion"].as_str().unwrap();
    let staged = evaluate(old, turn).unwrap();
    assert_eq!(owner_result(&staged), sample["reply"]);
    assert_eq!(staged["next_document"].as_str(), Some(expected));
    let replay = evaluate(expected, turn).unwrap();
    assert_eq!(owner_result(&replay), sample["reply"]);
    assert_eq!(replay["next_document"], Value::Null);
}

#[test]
fn legacy_confirmation_does_not_promote_retired_or_edited_candidates() {
    let sample = oracle();
    let prior: Value = serde_json::from_str(sample["document"].as_str().unwrap()).unwrap();
    let turn = &sample["turn"];
    for (path, change) in [
        ("/current_node_id", json!("new-head")),
        ("/lifecycle/active_user_message_id", json!("new-user")),
        ("/lifecycle/completion_candidate", Value::Null),
        (
            "/lifecycle/completion_candidate/completed_at_ms",
            json!(true),
        ),
        ("/messages/1/metadata/task_state", json!("waiting_user")),
        ("/messages/1/finish_reason", json!("cancelled")),
        ("/messages/1/content", json!("edited answer")),
    ] {
        let mut altered = prior.clone();
        let row = &mut altered["conversations"]["conversation-1"];
        // task_state can be absent in the Python message metadata.
        if path == "/messages/1/metadata/task_state" {
            row["messages"][1]["metadata"]["task_state"] = change;
        } else {
            *row.pointer_mut(path).unwrap() = change;
        }
        let staged = evaluate(&altered.to_string(), turn).unwrap();
        assert_eq!(owner_result(&staged)["confirmed"], false, "{path}");
        assert_eq!(owner_result(&staged)["store_revision"], prior["revision"]);
        assert_eq!(staged["next_document"], Value::Null, "{path}");
    }
    let mut removed = prior.clone();
    removed["conversations"]
        .as_object_mut()
        .unwrap()
        .remove("conversation-1");
    let reply = evaluate(&removed.to_string(), turn).unwrap();
    let reply = owner_result(&reply);
    assert_eq!(reply["confirmed"], false);
    assert_eq!(
        reply["conversation_revision"],
        turn["result_reference"]["conversation_revision"]
    );
}

#[test]
fn legacy_confirmation_requires_the_exact_durable_terminal_and_receipt() {
    let sample = oracle();
    let document = sample["document"].as_str().unwrap();
    for (field, invalid) in [
        ("profile_id", json!("other")),
        ("status", json!("running")),
        ("status", json!("cancelled")),
        ("revision", json!(true)),
        ("revision", json!(2)),
        ("request_id", json!("arbitrary")),
    ] {
        let mut turn = sample["turn"].clone();
        turn[field] = invalid;
        assert_eq!(evaluate(document, &turn), Err(ConfirmationError::Terminal));
    }
    for (field, invalid) in [
        ("input_digest", json!("unrelated")),
        ("conversation_revision", json!(20)),
        ("conversation_id", json!("other")),
    ] {
        let mut turn = sample["turn"].clone();
        turn[field] = invalid;
        assert_eq!(evaluate(document, &turn), Err(ConfirmationError::Receipt));
    }
    let mut turn = sample["turn"].clone();
    turn["result_reference"]["outcome_digest"] = json!("wrong");
    assert_eq!(evaluate(document, &turn), Err(ConfirmationError::Receipt));
}

#[test]
fn legacy_confirmation_accepts_evidence_field_order_and_refuses_opaque_codec() {
    let sample = oracle();
    let document = sample["document"].as_str().unwrap();
    let mut turn = loads(&sample["turn"].to_string()).unwrap();
    let PyValue::Object(fields) = &mut turn else {
        unreachable!();
    };
    let Some(PyValue::Object(reference)) = field(fields, "result_reference") else {
        unreachable!();
    };
    reference.reverse();
    let turn = dumps_bounded(&turn, MAX_BYTES).unwrap();
    let staged = confirm("defaults", document, &turn).unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&staged).unwrap()["next_document"],
        sample["promotion"]
    );
    let opaque = document.replace("Edited title", r"\uD800");
    assert_eq!(
        confirm("defaults", &opaque, &turn),
        Err(ConfirmationError::Read(ReadError::LegacyCodec))
    );
}
