use std::collections::BTreeSet;

use crate::exports::local::harness_chat2_0_0::conversation_store::{Context, Guest, Input};
use crate::local::harness_chat2_0_0::messages::{FinishReason, Message, MessagePart};
use crate::tobkiri::core1_1_0::{
    storage::{self, SqlResultPolicy, SqlTransaction, Store, TypedSqlResult},
    types as storage_types,
};
use crate::{Error, ErrorCode, MAX_LOAD_ROWS, Pack, STORE_NAME};

const FIND_SIDECAR: &str =
    "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'structured_messages'";
const CREATE_SIDECAR: &str = "CREATE TABLE IF NOT EXISTS structured_messages (
    seq INTEGER NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (seq, ordinal)
)";
const LOAD_LEGACY: &str = "SELECT role, content FROM messages WHERE conversation = ? ORDER BY seq";
const LOAD_JOINED: &str =
    "SELECT CASE WHEN s.seq IS NULL THEN m.role END, coalesce(s.payload, m.content)
    FROM messages AS m LEFT JOIN structured_messages AS s ON s.seq = m.seq
    WHERE m.conversation = ? ORDER BY m.seq, s.ordinal";
const INSERT_LEGACY: &str = "INSERT INTO messages (conversation, role, content) VALUES (?, ?, ?)";
const INSERT_STRUCTURED: &str =
    "INSERT INTO structured_messages (seq, ordinal, payload) VALUES (?, ?, ?)";

impl Guest for Pack {
    fn load(conversation: String) -> Result<Context, Error> {
        let store = storage::open(STORE_NAME).map_err(storage_error)?;
        let params = [text_cell(&conversation)];
        let rows = if has_sidecar(&store)? {
            store.sql_query_typed(LOAD_JOINED, &params, MAX_LOAD_ROWS)
        } else {
            let legacy = store
                .sql_query_typed(LOAD_LEGACY, &params, MAX_LOAD_ROWS)
                .map_err(storage_error)?;
            // The table is only created as part of a v2 commit. If it appeared
            // while loading legacy rows, read both tables in one new snapshot.
            if has_sidecar(&store)? {
                drop(legacy);
                store.sql_query_typed(LOAD_JOINED, &params, MAX_LOAD_ROWS)
            } else {
                Ok(legacy)
            }
        }
        .map_err(storage_error)?
        .rows;
        let messages = rows
            .iter()
            .map(|row| decode_message(row))
            .collect::<Result<_, _>>()?;
        Ok(Context {
            conversation,
            messages,
        })
    }

    fn append(conversation: String, input: Input, messages: Vec<Message>) -> Result<(), Error> {
        if input.conversation != conversation {
            return Err(invalid("conversation binding mismatch"));
        }
        if input.parts.is_empty()
            || input
                .parts
                .iter()
                .any(|part| !matches!(part, MessagePart::Text(_)))
        {
            return Err(invalid("user input requires text parts"));
        }
        let final_assistant = completed_assistant(&messages)?;
        let user_text = projected_text(&input.parts);
        let assistant_text = projected_text(&final_assistant.parts);
        let user = Message {
            role: "user".into(),
            parts: input.parts,
            response: None,
        };
        let store = storage::open(STORE_NAME).map_err(storage_error)?;
        let transaction = store.sql_begin().map_err(storage_error)?;
        let staged = stage_turn(
            &transaction,
            &conversation,
            &user,
            &user_text,
            &messages,
            &assistant_text,
        );
        if let Err(error) = staged {
            // Dropping the resource also lets Core finish cleanup if explicit
            // rollback cannot complete, for example after cancellation.
            let _ = transaction.rollback();
            return Err(error);
        }
        // Core may report a commit-unknown error. Return it verbatim and let
        // the resource drop clean up only transactions that remain active.
        transaction.commit().map_err(storage_error)
    }
}

fn completed_assistant(messages: &[Message]) -> Result<&Message, Error> {
    let Some((last, preceding)) = messages.split_last() else {
        return Err(invalid("turn requires a completed assistant response"));
    };
    if last.role != "assistant"
        || !last
            .response
            .as_ref()
            .is_some_and(|summary| !matches!(summary.finish, FinishReason::ToolCalls))
    {
        return Err(invalid("turn requires a completed assistant response"));
    }
    if last
        .parts
        .iter()
        .any(|part| !matches!(part, MessagePart::Text(_) | MessagePart::Reasoning(_)))
    {
        return Err(invalid(
            "final assistant response can only contain text and reasoning",
        ));
    }
    let mut pending_ids = BTreeSet::new();
    for message in preceding {
        match message.role.as_str() {
            "assistant" if pending_ids.is_empty() => {
                pending_ids = tool_call_ids(message)?;
            }
            "tool" if !pending_ids.is_empty() => {
                match_tool_results(message, &mut pending_ids)?;
            }
            _ => {
                return Err(invalid(
                    "turn contains an unexpected assistant or tool message",
                ));
            }
        }
    }
    if !pending_ids.is_empty() {
        return Err(invalid("turn is missing a tool result"));
    }
    Ok(last)
}

fn tool_call_ids(message: &Message) -> Result<BTreeSet<&str>, Error> {
    if !message
        .response
        .as_ref()
        .is_some_and(|summary| matches!(summary.finish, FinishReason::ToolCalls))
    {
        return Err(invalid(
            "intermediate assistant response requires a tool-call finish",
        ));
    }
    let mut ids = BTreeSet::new();
    for part in &message.parts {
        match part {
            MessagePart::Text(_) | MessagePart::Reasoning(_) => {}
            MessagePart::ToolCall(call)
                if !call.id.trim().is_empty() && ids.insert(call.id.as_str()) => {}
            _ => {
                return Err(invalid(
                    "intermediate assistant response contains an invalid tool call",
                ));
            }
        }
    }
    if ids.is_empty() {
        return Err(invalid(
            "intermediate assistant response requires a tool call",
        ));
    }
    Ok(ids)
}

fn match_tool_results(message: &Message, pending_ids: &mut BTreeSet<&str>) -> Result<(), Error> {
    if message.response.is_some() || message.parts.is_empty() {
        return Err(invalid(
            "tool message requires results and cannot carry a response summary",
        ));
    }
    for part in &message.parts {
        match part {
            MessagePart::ToolResult(result) if pending_ids.remove(result.call_id.as_str()) => {}
            _ => return Err(invalid("tool result does not match a pending tool call")),
        }
    }
    Ok(())
}

fn projected_text(parts: &[MessagePart]) -> String {
    parts
        .iter()
        .filter_map(|part| match part {
            MessagePart::Text(text) => Some(text.as_str()),
            _ => None,
        })
        .collect()
}

fn stage_turn(
    transaction: &SqlTransaction,
    conversation: &str,
    user: &Message,
    user_text: &str,
    messages: &[Message],
    assistant_text: &str,
) -> Result<(), Error> {
    execute(transaction, CREATE_SIDECAR, &[])?;
    let conversation = text_cell(conversation);
    let user_seq = insert_projection(transaction, &conversation, "user", user_text)?;
    let assistant_seq = insert_projection(transaction, &conversation, "assistant", assistant_text)?;
    insert_structured(transaction, user_seq, 0, user)?;
    for (ordinal, message) in messages.iter().enumerate() {
        insert_structured(transaction, assistant_seq, ordinal, message)?;
    }
    Ok(())
}

fn has_sidecar(store: &Store) -> Result<bool, Error> {
    store
        .sql_query_typed(FIND_SIDECAR, &[], 1)
        .map(|result| !result.rows.is_empty())
        .map_err(storage_error)
}

fn insert_projection(
    transaction: &SqlTransaction,
    conversation: &str,
    role: &str,
    content: &str,
) -> Result<i64, Error> {
    let result = execute(
        transaction,
        INSERT_LEGACY,
        &[conversation.to_owned(), text_cell(role), text_cell(content)],
    )?;
    if result.affected_rows.as_deref() != Some("1") {
        return Err(internal("conversation projection did not insert one row"));
    }
    result
        .last_insert_rowid
        .as_deref()
        .and_then(|rowid| rowid.parse::<i64>().ok())
        .ok_or_else(|| internal("conversation projection did not return a rowid"))
}

fn insert_structured(
    transaction: &SqlTransaction,
    seq: i64,
    ordinal: usize,
    message: &Message,
) -> Result<(), Error> {
    let payload =
        serde_json::to_string(message).map_err(|_| internal("cannot encode structured message"))?;
    let result = execute(
        transaction,
        INSERT_STRUCTURED,
        &[
            integer_cell(seq),
            integer_cell(ordinal),
            text_cell(&payload),
        ],
    )?;
    if result.affected_rows.as_deref() != Some("1") {
        return Err(internal("structured message did not insert one row"));
    }
    Ok(())
}

fn execute(
    transaction: &SqlTransaction,
    sql: &str,
    params: &[String],
) -> Result<TypedSqlResult, Error> {
    transaction
        .sql_exec_typed(sql, params, 0, SqlResultPolicy::ForbidRows)
        .map_err(storage_error)
}

#[derive(serde::Deserialize)]
#[serde(tag = "type", content = "value", rename_all = "lowercase")]
enum LoadedCell {
    Null(()),
    Text(String),
}

fn decode_message(row: &[String]) -> Result<Message, Error> {
    let [role, content] = row else {
        return Err(internal("invalid stored message row"));
    };
    let role = serde_json::from_str(role).map_err(|_| internal("invalid stored message role"))?;
    let LoadedCell::Text(content) =
        serde_json::from_str(content).map_err(|_| internal("invalid stored message content"))?
    else {
        return Err(internal("stored message content is not text"));
    };
    match role {
        LoadedCell::Null(()) => serde_json::from_str(&content)
            .map_err(|_| internal("invalid stored structured message")),
        LoadedCell::Text(role) => Ok(Message {
            role,
            parts: vec![MessagePart::Text(content)],
            response: None,
        }),
    }
}

fn text_cell(text: &str) -> String {
    serde_json::json!({ "type": "text", "value": text }).to_string()
}

fn integer_cell(integer: impl ToString) -> String {
    serde_json::json!({ "type": "integer", "value": integer.to_string() }).to_string()
}

fn invalid(message: &str) -> Error {
    Error {
        code: ErrorCode::Invalid,
        message: message.into(),
    }
}

fn internal(message: &str) -> Error {
    Error {
        code: ErrorCode::Internal,
        message: message.into(),
    }
}

fn storage_error(error: storage_types::Error) -> Error {
    use storage_types::ErrorCode as StorageCode;
    let code = match error.code {
        StorageCode::Denied => ErrorCode::Denied,
        StorageCode::ApprovalRequired => ErrorCode::ApprovalRequired,
        StorageCode::Cancelled => ErrorCode::Cancelled,
        StorageCode::NotFound => ErrorCode::NotFound,
        StorageCode::Invalid => ErrorCode::Invalid,
        StorageCode::LimitExceeded => ErrorCode::LimitExceeded,
        StorageCode::Conflict => ErrorCode::Conflict,
        StorageCode::Unavailable => ErrorCode::Unavailable,
        StorageCode::Internal => ErrorCode::Internal,
    };
    Error {
        code,
        message: error.message,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::local::harness_chat2_0_0::messages::{ResponseSummary, ToolCall, ToolResult, Usage};

    #[test]
    fn decodes_legacy_text_without_synthesizing_a_response() {
        let text = "雪\n山";
        let message = decode_message(&[text_cell("assistant"), text_cell(text)]).unwrap();
        assert_eq!(message.role, "assistant");
        assert!(matches!(&message.parts[..], [MessagePart::Text(value)] if value == text));
        assert!(message.response.is_none());
    }

    #[test]
    fn structured_payload_preserves_parts_call_ids_and_full_width_usage() {
        let message = Message {
            role: "assistant".into(),
            parts: vec![
                MessagePart::Text("a".into()),
                MessagePart::Reasoning("thought".into()),
                MessagePart::ToolCall(ToolCall {
                    id: "call-1".into(),
                    name: "rewrite".into(),
                    arguments: r#"{"text":"雪"}"#.into(),
                }),
                MessagePart::ToolResult(ToolResult {
                    call_id: "call-1".into(),
                    content: "山".into(),
                    is_error: true,
                }),
                MessagePart::Text("b".into()),
            ],
            response: Some(ResponseSummary {
                finish: FinishReason::Other("provider-finish".into()),
                usage: Some(Usage {
                    prompt_tokens: Some(0),
                    completion_tokens: None,
                    cached_tokens: Some(u64::MAX),
                }),
            }),
        };
        let payload = serde_json::to_string(&message).unwrap();
        let decoded = decode_message(&[
            r#"{"type":"null","value":null}"#.into(),
            text_cell(&payload),
        ])
        .unwrap();
        assert_eq!(serde_json::to_string(&decoded).unwrap(), payload);
        assert_eq!(projected_text(&decoded.parts), "ab");
        let usage = decoded.response.unwrap().usage.unwrap();
        assert_eq!(usage.prompt_tokens, Some(0));
        assert_eq!(usage.completion_tokens, None);
        assert_eq!(usage.cached_tokens, Some(u64::MAX));
    }

    fn assistant(finish: Option<FinishReason>, parts: Vec<MessagePart>) -> Message {
        Message {
            role: "assistant".into(),
            parts,
            response: finish.map(|finish| ResponseSummary {
                finish,
                usage: None,
            }),
        }
    }

    fn call(id: &str) -> MessagePart {
        MessagePart::ToolCall(ToolCall {
            id: id.into(),
            name: "rewrite".into(),
            arguments: "{}".into(),
        })
    }

    fn tool_message(id: &str) -> Message {
        Message {
            role: "tool".into(),
            parts: vec![MessagePart::ToolResult(ToolResult {
                call_id: id.into(),
                content: "result".into(),
                is_error: false,
            })],
            response: None,
        }
    }

    #[test]
    fn turn_requires_a_finished_text_or_reasoning_assistant() {
        assert!(completed_assistant(&[]).is_err());
        assert!(completed_assistant(&[assistant(None, vec![])]).is_err());
        assert!(
            completed_assistant(&[assistant(Some(FinishReason::ToolCalls), vec![call("a")])])
                .is_err()
        );
        assert!(
            completed_assistant(&[assistant(Some(FinishReason::Stop), vec![call("a")])]).is_err()
        );
    }

    #[test]
    fn accepts_multiple_tool_rounds_with_adjacent_result_messages() {
        let messages = [
            assistant(Some(FinishReason::ToolCalls), vec![call("a"), call("b")]),
            tool_message("b"),
            tool_message("a"),
            assistant(Some(FinishReason::ToolCalls), vec![call("a")]),
            tool_message("a"),
            assistant(
                Some(FinishReason::Stop),
                vec![MessagePart::Text("done".into())],
            ),
        ];
        let final_assistant = completed_assistant(&messages).unwrap();
        assert_eq!(projected_text(&final_assistant.parts), "done");
        assert!(final_assistant.response.as_ref().unwrap().usage.is_none());
    }

    #[test]
    fn rejects_missing_unrelated_and_duplicate_tool_ids() {
        for results in [vec![], vec!["a"], vec!["a", "x"], vec!["a", "a"]] {
            let mut messages = vec![assistant(
                Some(FinishReason::ToolCalls),
                vec![call("a"), call("b")],
            )];
            messages.extend(results.into_iter().map(tool_message));
            messages.push(assistant(Some(FinishReason::Stop), vec![]));
            assert!(completed_assistant(&messages).is_err());
        }
        let duplicate_calls = [
            assistant(Some(FinishReason::ToolCalls), vec![call("a"), call("a")]),
            tool_message("a"),
            assistant(Some(FinishReason::Stop), vec![]),
        ];
        assert!(completed_assistant(&duplicate_calls).is_err());
    }
}
