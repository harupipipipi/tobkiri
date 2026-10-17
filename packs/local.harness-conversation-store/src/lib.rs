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

// Flow's stream.collect passes chat-event values, not enclosing Flow frames.
// A suspended segment has no assistant answer and must not append an empty turn.
fn completed_turn(conversation: &str, events: &[ChatEvent]) -> Result<Option<String>, Error> {
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
}
export!(Pack);
