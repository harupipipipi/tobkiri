//! Conversation persistence using only the Pack's private core store.
#[cfg(not(feature = "structured-chat"))]
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
#[cfg(feature = "structured-chat")]
wit_bindgen::generate!({
    path: "structured-wit",
    world: "pack",
    generate_all,
    additional_type_attributes: {
        "local:harness-chat/messages@2.0.0/message": [#[derive(serde::Serialize, serde::Deserialize)]],
        "local:harness-chat/messages@2.0.0/message-part": [#[derive(serde::Serialize, serde::Deserialize)]],
        "local:harness-chat/messages@2.0.0/tool-call": [#[derive(serde::Serialize, serde::Deserialize)]],
        "local:harness-chat/messages@2.0.0/tool-result": [#[derive(serde::Serialize, serde::Deserialize)]],
        "local:harness-chat/messages@2.0.0/response-summary": [#[derive(serde::Serialize, serde::Deserialize)]],
        "local:harness-chat/messages@2.0.0/finish-reason": [#[derive(serde::Serialize, serde::Deserialize)]],
        "local:harness-chat/messages@2.0.0/usage": [#[derive(serde::Serialize, serde::Deserialize)]],
    },
});

#[cfg(feature = "structured-chat")]
mod structured;

#[cfg(not(feature = "structured-chat"))]
use exports::local::harness_chat::conversation_store::{ChatEvent, Context, Guest, Input};
#[cfg(feature = "structured-chat")]
use exports::local::harness_chat1_0_0::conversation_store::{ChatEvent, Context, Guest, Input};
#[cfg(not(feature = "structured-chat"))]
use local::harness_chat::messages::Message;
#[cfg(feature = "structured-chat")]
use local::harness_chat1_0_0::messages::Message;
#[cfg(not(feature = "structured-chat"))]
use tobkiri::core as core_v1;
#[cfg(feature = "structured-chat")]
use tobkiri::core1_0_0 as core_v1;

use core_v1::{
    storage,
    types::{Error, ErrorCode},
};
#[cfg(not(feature = "structured-chat"))]
use exports::tobkiri::core::lifecycle;
#[cfg(feature = "structured-chat")]
use exports::tobkiri::core1_0_0::lifecycle;

const STORE_NAME: &str = "conversations";
const MAX_LOAD_ROWS: u32 = 1000;

struct Pack;
impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        let store = storage::open(STORE_NAME)?;
        store.sql_exec(
            "CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)",
            &[],
        )?;
        Ok(())
    }

    fn shutdown() {}
}
impl Guest for Pack {
    fn load(conversation: String) -> Result<Context, Error> {
        let store = storage::open(STORE_NAME)?;
        let rows = store.sql_query(
            "SELECT role, content FROM messages WHERE conversation = ? ORDER BY seq",
            &[serde_json::to_string(&conversation).unwrap()],
            MAX_LOAD_ROWS,
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
        let completed = events
            .iter()
            .rev()
            .find(|e| e.kind == "done")
            .and_then(|e| e.text.clone())
            .ok_or_else(|| Error {
                code: ErrorCode::Invalid,
                message: "incomplete stream cannot be saved as a turn".into(),
            })?;
        let store = storage::open(STORE_NAME)?;
        // One atomic statement preserves both messages or neither.
        store.sql_exec("INSERT INTO messages (conversation, role, content) VALUES (?, 'user', ?), (?, 'assistant', ?)",&[serde_json::to_string(&conversation).unwrap(),serde_json::to_string(&input.text).unwrap(),serde_json::to_string(&conversation).unwrap(),serde_json::to_string(&completed).unwrap()])?;
        Ok(())
    }
}
export!(Pack);
