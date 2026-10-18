//! Conversation persistence using only the Pack's private core store.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use exports::local::harness_chat::conversation_store::{ChatEvent, Context, Guest, Input};
use exports::tobkiri::core::lifecycle;
use local::harness_chat::messages::Message;
use tobkiri::core::{
    storage,
    types::{Error, ErrorCode},
};
struct Pack;

// This is a different boundary from a successfully completed phase. Zero new
// phases are allowed here, but an answer, unsettled attempt or mixed boundary is
// never appended as a conversation turn.
fn awaiting_retry_turn(conversation: &str, events: &[ChatEvent]) -> Result<bool, Error> {
    if !events.iter().any(|event| event.kind == "awaiting_retry") {
        return Ok(false);
    }
    let invalid = || Error {
        code: ErrorCode::Invalid,
        message: "incomplete or conflicting contract retry cannot be saved as a turn".into(),
    };
    if events
        .iter()
        .filter(|event| event.kind == "awaiting_retry")
        .count()
        != 1
        || events
            .last()
            .is_none_or(|event| event.kind != "awaiting_retry")
        || events
            .iter()
            .any(|event| !matches!(event.kind.as_str(), "progress" | "usage" | "awaiting_retry"))
    {
        return Err(invalid());
    }
    let receipt: serde_json::Value = serde_json::from_str(
        events
            .last()
            .and_then(|event| event.text.as_deref())
            .ok_or_else(invalid)?,
    )
    .map_err(|_| invalid())?;
    let keys = [
        "version",
        "retry_boundary_version",
        "run_id",
        "conversation",
        "status",
        "approved",
        "phase",
        "next_phase",
        "completed_phases",
        "completed_phases_this_segment",
        "reason",
        "rejected_attempt",
        "rejected_generation",
        "prepared_generation",
        "retry",
        "maximum_retries",
        "usage",
    ];
    let rejected = receipt["rejected_generation"]
        .as_str()
        .ok_or_else(invalid)?;
    let (base, epoch) = rejected.rsplit_once('#').ok_or_else(invalid)?;
    let number = epoch.parse::<u32>().map_err(|_| invalid())?;
    let next = number.checked_add(1).ok_or_else(invalid)?;
    let phase = receipt["phase"]
        .as_str()
        .filter(|phase| !phase.trim().is_empty())
        .ok_or_else(invalid)?;
    let attempt = receipt["rejected_attempt"].as_str().ok_or_else(invalid)?;
    let ordinal = attempt
        .strip_prefix(&format!("{rejected}:"))
        .ok_or_else(invalid)?;
    let ordinal_number = ordinal.parse::<u64>().map_err(|_| invalid())?;
    let retry = receipt["retry"].as_u64().ok_or_else(invalid)?;
    let maximum = receipt["maximum_retries"].as_u64().ok_or_else(invalid)?;
    if receipt.as_object().is_none_or(|fields| {
        fields.len() != keys.len() || fields.keys().any(|key| !keys.contains(&key.as_str()))
    }) || receipt["version"] != "deepthink.workflow.v5"
        || receipt["retry_boundary_version"] != "deepthink.contract-retry-boundary.v1"
        || receipt["status"] != "awaiting_retry"
        || receipt["approved"] != false
        || receipt["reason"] != "invalid_phase_contract"
        || receipt["conversation"] != conversation
        || receipt["run_id"].as_str().is_none_or(|id| {
            id.len() != 64
                || !id
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        || receipt["next_phase"] != phase
        || receipt["completed_phases"].as_u64().is_none()
        || receipt["completed_phases_this_segment"] != 0
        || !base.starts_with(&format!("{phase}@"))
        || number.to_string() != epoch
        || ordinal_number.to_string() != ordinal
        || receipt["prepared_generation"] != format!("{base}#{next}")
        || retry == 0
        || retry > maximum
        || maximum > 3
        || !receipt["usage"].is_object()
        || receipt["usage"]["pending_attempts"] != 0
    {
        return Err(invalid());
    }
    Ok(true)
}

// Flow's stream.collect passes chat-event values, not enclosing Flow frames.
// A suspended segment has no assistant answer and must not append an empty turn.
fn completed_turn(conversation: &str, events: &[ChatEvent]) -> Result<Option<String>, Error> {
    if awaiting_retry_turn(conversation, events)? {
        return Ok(None);
    }
    let invalid = || Error {
        code: ErrorCode::Invalid,
        message: "incomplete or conflicting stream cannot be saved as a turn".into(),
    };
    let paused = events
        .iter()
        .filter(|event| event.kind == "paused")
        .collect::<Vec<_>>();
    if paused.is_empty() {
        return events
            .iter()
            .rev()
            .find(|event| event.kind == "done")
            .and_then(|event| event.text.clone())
            .map(Some)
            .ok_or_else(invalid);
    }
    if paused.len() != 1
        || events.last().is_none_or(|event| event.kind != "paused")
        || events
            .iter()
            .any(|event| !matches!(event.kind.as_str(), "progress" | "usage" | "paused"))
    {
        return Err(invalid());
    }
    let receipt: serde_json::Value =
        serde_json::from_str(paused[0].text.as_deref().ok_or_else(invalid)?)
            .map_err(|_| invalid())?;
    let keys = [
        "version",
        "run_id",
        "conversation",
        "status",
        "approved",
        "next_phase",
        "completed_phases",
        "usage",
    ];
    if receipt.as_object().is_none_or(|fields| {
        fields.len() != keys.len() || fields.keys().any(|key| !keys.contains(&key.as_str()))
    }) || receipt["version"] != "deepthink.workflow.v5"
        || receipt["status"] != "paused"
        || receipt["approved"] != false
        || receipt["conversation"] != conversation
        || receipt["run_id"].as_str().is_none_or(|id| {
            id.len() != 64
                || !id
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        || receipt["next_phase"]
            .as_str()
            .is_none_or(|phase| phase.trim().is_empty())
        || receipt["completed_phases"]
            .as_u64()
            .is_none_or(|count| count == 0)
        || !receipt["usage"].is_object()
        || receipt["usage"]["pending_attempts"] != 0
    {
        return Err(invalid());
    }
    Ok(None)
}

fn initialize() -> Result<storage::Store, Error> {
    let store = storage::open("conversations")?;
    store.sql_exec("CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)",&[])?;
    Ok(store)
}
impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        initialize()?;
        Ok(())
    }
    fn shutdown() {}
}
impl Guest for Pack {
    fn load(conversation: String) -> Result<Context, Error> {
        let store = initialize()?;
        let rows = store.sql_query(
            "SELECT role, content FROM messages WHERE conversation = ? ORDER BY seq",
            &[serde_json::to_string(&conversation).unwrap()],
            1000,
        )?;
        let mut messages = Vec::new();
        for row in rows {
            if row.len() != 2 {
                return Err(Error {
                    code: ErrorCode::Internal,
                    message: "invalid stored message".into(),
                });
            }
            messages.push(Message {
                role: serde_json::from_str(&row[0]).map_err(|_| Error {
                    code: ErrorCode::Invalid,
                    message: "invalid role".into(),
                })?,
                content: serde_json::from_str(&row[1]).map_err(|_| Error {
                    code: ErrorCode::Invalid,
                    message: "invalid content".into(),
                })?,
            });
        }
        Ok(Context {
            conversation,
            messages,
        })
    }
    fn append(conversation: String, input: Input, events: Vec<ChatEvent>) -> Result<(), Error> {
        if input.conversation != conversation {
            return Err(Error {
                code: ErrorCode::Invalid,
                message: "conversation binding mismatch".into(),
            });
        }
        let Some(completed) = completed_turn(&conversation, &events)? else {
            return Ok(());
        };
        let store = initialize()?;
        // One atomic statement preserves both messages or neither.
        store.sql_exec("INSERT INTO messages (conversation, role, content) VALUES (?, 'user', ?), (?, 'assistant', ?)",&[serde_json::to_string(&conversation).unwrap(),serde_json::to_string(&input.text).unwrap(),serde_json::to_string(&conversation).unwrap(),serde_json::to_string(&completed).unwrap()])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn event(kind: &str, text: Option<String>) -> ChatEvent {
        ChatEvent {
            kind: kind.into(),
            text,
            prompt_tokens: None,
            completion_tokens: None,
        }
    }

    fn receipt() -> Value {
        json!({"version":"deepthink.workflow.v5","run_id":"a".repeat(64),
            "conversation":"fixture","status":"paused","approved":false,
            "next_phase":"goal_contract","completed_phases":1,
            "usage":{"pending_attempts":0,"tokens_complete":false,"cost_known":false}})
    }

    fn paused(receipt: &Value) -> Vec<ChatEvent> {
        vec![
            event(
                "progress",
                Some(json!({"phase":"goal_intake","status":"completed"}).to_string()),
            ),
            event("usage", Some(receipt["usage"].to_string())),
            event("progress", Some(receipt.to_string())),
            event("paused", Some(receipt.to_string())),
        ]
    }

    #[test]
    fn plain_harness_done_keeps_its_existing_answer_behavior() {
        let events = vec![
            event("delta", Some("391".into())),
            event("done", Some("391".into())),
        ];
        assert_eq!(
            completed_turn("fixture", &events).unwrap(),
            Some("391".into())
        );
        assert_eq!(
            completed_turn("fixture", &[event("done", Some(String::new()))]).unwrap(),
            Some(String::new())
        );
        assert!(completed_turn("fixture", &[event("progress", None)]).is_err());
    }

    #[test]
    fn collected_paused_segment_does_not_open_or_write_conversation_storage() {
        let events = paused(&receipt());
        assert_eq!(completed_turn("fixture", &events).unwrap(), None);
        // Native unit tests cannot perform a Core import. This must return
        // before initialize()/storage::open, rather than inserting an empty pair.
        <Pack as Guest>::append(
            "fixture".into(),
            Input {
                conversation: "fixture".into(),
                text: "original request".into(),
            },
            events,
        )
        .unwrap();
    }

    #[test]
    fn paused_receipt_cannot_mix_with_answer_or_terminal_events() {
        for kind in [
            "delta",
            "done",
            "failed",
            "cancelled",
            "completed",
            "emitted",
        ] {
            let mut events = paused(&receipt());
            events.insert(0, event(kind, Some("unapproved candidate".into())));
            assert!(completed_turn("fixture", &events).is_err(), "{kind}");
        }
        let mut duplicated = paused(&receipt());
        duplicated.push(event("paused", Some(receipt().to_string())));
        assert!(completed_turn("fixture", &duplicated).is_err());
    }

    #[test]
    fn invalid_or_unsettled_pause_receipt_is_not_a_successful_turn() {
        for (key, value) in [
            ("version", json!("deepthink.workflow.v4")),
            ("run_id", json!("unknown")),
            ("conversation", json!("another")),
            ("approved", json!(true)),
            ("status", json!("needs_reconciliation")),
            ("next_phase", json!(null)),
            ("completed_phases", json!(0)),
            ("usage", json!({"pending_attempts":1})),
        ] {
            let mut invalid = receipt();
            invalid[key] = value;
            assert!(
                completed_turn("fixture", &paused(&invalid)).is_err(),
                "{key}"
            );
        }
        let mut extra = receipt();
        extra["output"] = json!("not approved");
        assert!(completed_turn("fixture", &paused(&extra)).is_err());
        assert!(completed_turn("fixture", &[event("paused", Some("broken".into()))]).is_err());
    }

    fn retry_receipt() -> Value {
        json!({"version":"deepthink.workflow.v5","retry_boundary_version":"deepthink.contract-retry-boundary.v1",
            "run_id":"a".repeat(64),"conversation":"fixture","status":"awaiting_retry","approved":false,
            "phase":"goal_intake","next_phase":"goal_intake","completed_phases":0,"completed_phases_this_segment":0,
            "reason":"invalid_phase_contract","rejected_attempt":"goal_intake@0:0#0:0",
            "rejected_generation":"goal_intake@0:0#0","prepared_generation":"goal_intake@0:0#1",
            "retry":1,"maximum_retries":2,"usage":{"pending_attempts":0,"cost_known":false,"cost_usd":null}})
    }

    fn awaiting(receipt: &Value) -> Vec<ChatEvent> {
        vec![
            event("progress", Some(receipt.to_string())),
            event("usage", Some(receipt["usage"].to_string())),
            event("awaiting_retry", Some(receipt.to_string())),
        ]
    }

    #[test]
    fn retry_with_zero_completed_phases_does_not_initialize_or_append_history() {
        let events = awaiting(&retry_receipt());
        assert_eq!(completed_turn("fixture", &events).unwrap(), None);
        <Pack as Guest>::append(
            "fixture".into(),
            Input {
                conversation: "fixture".into(),
                text: "original request".into(),
            },
            events,
        )
        .unwrap();
        // The ordinary completed-phase pause contract stays strict.
        let mut invalid_pause = receipt();
        invalid_pause["completed_phases"] = json!(0);
        assert!(completed_turn("fixture", &paused(&invalid_pause)).is_err());
    }

    #[test]
    fn retry_boundary_rejects_answers_mixed_boundaries_and_unsettled_usage() {
        for kind in [
            "delta",
            "done",
            "paused",
            "completed",
            "failed",
            "cancelled",
            "awaiting_retry",
        ] {
            let mut events = awaiting(&retry_receipt());
            events.insert(0, event(kind, Some("not an answer".into())));
            assert!(completed_turn("fixture", &events).is_err(), "{kind}");
        }
        for (field, value) in [
            ("completed_phases_this_segment", json!(1)),
            ("approved", json!(true)),
            ("retry", json!(0)),
            ("retry", json!(3)),
            ("maximum_retries", json!(4)),
            ("next_phase", json!("goal_contract")),
            ("prepared_generation", json!("goal_intake@0:0#0")),
            ("rejected_attempt", json!("goal_intake@0:0#1:1")),
            ("usage", json!({"pending_attempts":1})),
            ("reason", json!("transport_unknown")),
        ] {
            let mut changed = retry_receipt();
            changed[field] = value;
            assert!(
                completed_turn("fixture", &awaiting(&changed)).is_err(),
                "{field}"
            );
        }
        let mut extra = retry_receipt();
        extra["output"] = json!("unapproved");
        assert!(completed_turn("fixture", &awaiting(&extra)).is_err());
    }
}
export!(Pack);
