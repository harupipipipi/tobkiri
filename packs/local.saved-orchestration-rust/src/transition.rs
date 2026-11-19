use serde_json::{Value, json};
use tobkiri_canon::Digest;

use crate::{
    Error, Result, STATE_LIMIT, canonical_bytes, check_canonical_size, intent, py_truthy, validate,
};

const USER_LIMIT: usize = 3 * 1024 * 1024;
const AI_LIMIT: usize = 60 * 1024;
const OWNER_LIMIT: usize = 512 * 1024;
const MAX_TOOL_CALLS: usize = 8;
const STATE_FIELDS: [&str; 16] = [
    "hop",
    "request",
    "model_reference",
    "revision",
    "parent_id",
    "assistant",
    "assistant_finish_reason",
    "stage",
    "pending_tools",
    "tool_messages",
    "tool_count",
    "seen_tools",
    "system_prompt_digest",
    "user_content",
    "user_content_digest",
    "ai_mode",
];

enum Step {
    Intent(Option<Value>),
    Complete(Value),
    Failure(&'static str),
}

/// Request an owned conversation before attempting a write.
pub(crate) fn start(mut input: Value) -> Result<Value> {
    validate::request(&input)?;
    let revision = input["conversation_revision"].clone();
    let user_content = input
        .as_object_mut()
        .and_then(|fields| fields.remove("content"))
        .ok_or(Error::Invalid(
            "saved conversation user content is unavailable",
        ))?;
    intent::intent(
        json!({
            "hop": 0,
            "request": input,
            "model_reference": null,
            "revision": revision,
            "parent_id": null,
            "assistant": null,
            "assistant_finish_reason": null,
            "stage": "read",
            "pending_tools": [],
            "tool_messages": [],
            "tool_count": 0,
            "seen_tools": [],
            "system_prompt_digest": null,
            "user_content": user_content,
            "user_content_digest": null,
            "ai_mode": "buffered",
        }),
        None,
    )
}

/// Advance once from an authenticated owner outcome.
pub(crate) fn resume(mut state: Value, outcome: Value) -> Result<Value> {
    check_canonical_size(&state, STATE_LIMIT)?;
    let fields = state
        .as_object_mut()
        .ok_or(Error::Invalid("saved continuation fields are invalid"))?;
    let legacy = !fields.contains_key("ai_mode");
    if legacy {
        fields.insert("ai_mode".into(), json!("buffered"));
    }
    if fields.len() != STATE_FIELDS.len()
        || !STATE_FIELDS.iter().all(|key| fields.contains_key(*key))
    {
        return Err(Error::Invalid("saved continuation fields are invalid"));
    }
    if legacy {
        check_canonical_size(&state, STATE_LIMIT)?;
    }
    validate::state_request(&state["request"])?;
    let stage = state["stage"]
        .as_str()
        .filter(|stage| matches!(*stage, "read" | "user" | "ai" | "assistant" | "tool"))
        .ok_or(Error::Invalid("saved continuation hop is invalid"))?
        .to_owned();
    let hop = state["hop"]
        .as_u64()
        .filter(|hop| *hop < 20)
        .ok_or(Error::Invalid("saved continuation hop is invalid"))?;
    let revision = validate::revision(&state["revision"])?;

    let Value::Object(mut outcome) = outcome else {
        return intent::failure(&state, &stage, "HOST_ACTION_FAILED");
    };
    if outcome.len() != 2 || outcome.get("status").and_then(Value::as_str) != Some("ok") {
        return intent::failure(&state, &stage, "HOST_ACTION_FAILED");
    }
    let Some(value) = outcome.remove("value") else {
        return intent::failure(&state, &stage, "HOST_ACTION_FAILED");
    };
    let limit = match stage.as_str() {
        "user" => USER_LIMIT,
        "ai" => AI_LIMIT,
        _ => OWNER_LIMIT,
    };
    match check_canonical_size(&value, limit)
        .and_then(|()| advance(&mut state, &stage, revision, value))
    {
        Ok(Step::Complete(value)) => Ok(value),
        Ok(Step::Failure(code)) => intent::failure(&state, &stage, code),
        Err(_) => intent::failure(&state, &stage, "OWNER_RESPONSE_INVALID"),
        Ok(Step::Intent(user_content)) => {
            // The next intent might exceed its budget before it can be dispatched.
            // A validated user append is already saved at that point.
            let persistence_stage = if stage == "user" { "ai" } else { &stage };
            let failure = intent::failure(&state, persistence_stage, "OWNER_RESPONSE_INVALID");
            state["hop"] = json!(hop + 1);
            intent::intent(state, user_content).or(failure)
        }
    }
}

fn advance(state: &mut Value, stage: &str, revision: i64, value: Value) -> Result<Step> {
    if !value.is_object() {
        return Err(Error::Invalid("owner response must be an object"));
    }
    match stage {
        "read" => read_conversation(state, value),
        "user" | "assistant" => acknowledge_message(state, stage, revision, value),
        "tool" => acknowledge_tool(state, value),
        "ai" => complete_ai(state, value),
        _ => Err(Error::Invalid("saved continuation hop is invalid")),
    }
}

fn read_conversation(state: &mut Value, value: Value) -> Result<Step> {
    let request = &state["request"];
    let conversation = &value["conversation"];
    if conversation["id"] != request["conversation_id"] {
        return Err(Error::Invalid("conversation identity mismatch"));
    }
    let Some(messages) = conversation["messages"].as_array() else {
        return Ok(Step::Failure("TURN_RECONCILIATION_REQUIRED"));
    };
    let user_id = intent::message_id(request, "user")?;
    let assistant_id = intent::message_id(request, "assistant")?;
    for message in messages {
        if !message.is_object() {
            return Ok(Step::Failure("TURN_RECONCILIATION_REQUIRED"));
        }
        let id = &message["id"];
        if id.is_object() || id.is_array() {
            return Err(Error::Invalid("conversation message identity is invalid"));
        }
        if id.as_str() == Some(user_id.as_str()) || id.as_str() == Some(assistant_id.as_str()) {
            return Ok(Step::Failure("TURN_RECONCILIATION_REQUIRED"));
        }
    }
    if json!(validate::revision(&conversation["conversation_revision"])?)
        != request["conversation_revision"]
    {
        return Ok(Step::Failure("CONVERSATION_REVISION_CONFLICT"));
    }
    if py_truthy(&conversation["agent_id"]) {
        return Ok(Step::Failure("CONTEXT_RESOLUTION_REQUIRED"));
    }
    let model = &conversation["model_reference"];
    if !model.as_str().is_some_and(validate::py_nonblank) {
        return Ok(Step::Failure("MODEL_REFERENCE_REQUIRED"));
    }
    let parent = &conversation["current_node_id"];
    if !parent.is_null() {
        validate::identifier(parent)?;
    }
    state["parent_id"] = parent.clone();
    if py_truthy(&conversation["system_prompt_id"]) {
        let digest = &value["system_prompt_digest"];
        if !digest.as_str().is_some_and(validate::is_digest) {
            return Ok(Step::Failure("CONTEXT_RESOLUTION_REQUIRED"));
        }
        state["system_prompt_digest"] = digest.clone();
    } else if value.get("system_prompt_digest").is_some() {
        return Err(Error::Invalid("unexpected saved system prompt"));
    }
    state["model_reference"] = model.clone();
    let mode = match value.get("delivery_mode") {
        None => "buffered",
        Some(Value::String(mode)) if matches!(mode.as_str(), "buffered" | "incremental") => mode,
        _ => return Err(Error::Invalid("saved AI delivery mode is invalid")),
    };
    state["ai_mode"] = json!(mode);
    if !validate::saved_user_content(&state["user_content"])
        || !state["user_content_digest"].is_null()
    {
        return Err(Error::Invalid(
            "saved conversation user content is unavailable",
        ));
    }
    let content = state["user_content"].take();
    state["user_content_digest"] = json!(user_content_digest(&content)?.as_str());
    state["stage"] = json!("user");
    Ok(Step::Intent(Some(content)))
}

fn user_content_digest(content: &Value) -> Result<Digest> {
    Ok(Digest::hash(&canonical_bytes(content, USER_LIMIT)?))
}

fn acknowledge_message(
    state: &mut Value,
    stage: &str,
    revision: i64,
    mut value: Value,
) -> Result<Step> {
    let request = &state["request"];
    let message = value["message"].take();
    let expected = if stage == "assistant" {
        Some(intent::message(state, stage, None)?)
    } else {
        None
    };
    let mismatch = Error::Invalid("message owner acknowledgement mismatch");
    if !message.is_object() || value["action"] != "message_appended" {
        return Err(mismatch);
    }
    if let Some(expected) = expected {
        let expected = expected
            .as_object()
            .ok_or(Error::Invalid("saved message is invalid"))?;
        if expected
            .iter()
            .any(|(key, item)| !python_equal(&message[key], item))
        {
            return Err(mismatch);
        }
    } else if message["id"] != intent::message_id(request, "user")?
        || message["role"] != "user"
        || !python_equal(&message["parent_id"], &state["parent_id"])
        || !python_equal(&message["metadata"], &intent::message_metadata(request)?)
        || message["status"] != "complete"
        || !validate::saved_user_content(&message["content"])
        || state["user_content_digest"] != user_content_digest(&message["content"])?.as_str()
    {
        return Err(mismatch);
    }
    let owner_revision = validate::revision(&value["conversation_revision"])?;
    if owner_revision <= revision {
        return Err(Error::Invalid("message owner revision did not advance"));
    }
    if stage == "assistant" {
        return Ok(Step::Complete(json!({
            "status": "ok",
            "turn_id": request["turn_id"],
            "conversation_id": request["conversation_id"],
            "conversation_revision": owner_revision,
            "user_message_id": intent::message_id(request, "user")?,
            "message": message,
        })));
    }
    state["revision"] = json!(owner_revision);
    state["stage"] = json!("ai");
    Ok(Step::Intent(None))
}

// Owner acknowledgements compare decoded Python values. In particular, JSON
// booleans compare equal to the integers 0 and 1, including inside metadata.
fn python_equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Bool(value), Value::Number(number))
        | (Value::Number(number), Value::Bool(value)) => number.as_i64() == Some(i64::from(*value)),
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| python_equal(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, left)| {
                    right
                        .get(key)
                        .is_some_and(|right| python_equal(left, right))
                })
        }
        _ => left == right,
    }
}

fn state_array(value: &mut Value) -> Result<&mut Vec<Value>> {
    value
        .as_array_mut()
        .ok_or(Error::Invalid("saved tool continuation state is invalid"))
}

fn acknowledge_tool(state: &mut Value, mut value: Value) -> Result<Step> {
    let tool = state["pending_tools"]
        .get(0)
        .ok_or(Error::Invalid("saved tool continuation is unavailable"))?;
    if !python_equal(&value["tool_id"], &tool["tool_id"])
        || !python_equal(&value["tool_call_id"], &tool["tool_call_id"])
        || !value["content"].is_string()
    {
        return Err(Error::Invalid("tool owner acknowledgement mismatch"));
    }
    let message = json!({
        "role": "tool",
        "tool_call_id": tool["tool_call_id"],
        "content": value["content"].take(),
    });
    state_array(&mut state["tool_messages"])?.push(message);
    let pending = state_array(&mut state["pending_tools"])?;
    pending.remove(0);
    let next_stage = if pending.is_empty() { "ai" } else { "tool" };
    let count = state["tool_count"]
        .as_i64()
        .or_else(|| state["tool_count"].as_bool().map(i64::from))
        .ok_or(Error::Invalid("saved tool count is invalid"))?;
    state["tool_count"] = json!(count + 1);
    state["stage"] = json!(next_stage);
    Ok(Step::Intent(None))
}

fn complete_ai(state: &mut Value, mut value: Value) -> Result<Step> {
    if value["status"] != "ok" {
        return Ok(Step::Failure("AI_COMPLETION_UNAVAILABLE"));
    }
    let outcome = value
        .as_object_mut()
        .ok_or(Error::Invalid("owner response must be an object"))?;
    let intents = match outcome.remove("tool_intents") {
        None => Vec::new(),
        Some(Value::Array(intents)) => intents,
        _ => return Err(Error::Invalid("tool intents are invalid")),
    };
    let selection = &state["request"]["tool_selection"];
    let output = outcome.remove("output").unwrap_or(Value::Null);
    if intents.is_empty() {
        if py_truthy(&selection["must_use"]) && !py_truthy(&state["tool_count"]) {
            return Ok(Step::Failure("REQUIRED_TOOL_NOT_USED"));
        }
        if (!output.is_string() && !output.is_array()) || !py_truthy(&output) {
            return Err(Error::Invalid("AI output is invalid"));
        }
        state["assistant"] = output;
        let finish = outcome.remove("finish_reason").unwrap_or(Value::Null);
        if !finish.is_null()
            && !matches!(
                finish.as_str(),
                Some(
                    "stop"
                        | "waiting_user"
                        | "waiting_approval"
                        | "cancelled"
                        | "error"
                        | "running"
                )
            )
        {
            return Err(Error::Invalid("AI terminal state is invalid"));
        }
        state["assistant_finish_reason"] = finish;
        state["stage"] = json!("assistant");
    } else {
        let definitions = outcome.get("tool_definitions").and_then(Value::as_object);
        let Some(definitions) =
            definitions.filter(|_| !matches!(selection["mode"].as_str(), None | Some("none")))
        else {
            return Ok(Step::Failure("AI_COMPLETION_UNAVAILABLE"));
        };
        if state_array(&mut state["seen_tools"])?.len() + intents.len() > MAX_TOOL_CALLS {
            return Ok(Step::Failure("AI_COMPLETION_UNAVAILABLE"));
        }
        let mut calls = Vec::with_capacity(intents.len());
        for mut call in intents {
            let invalid = Error::Invalid("tool intent binding is invalid");
            let arguments = call
                .as_object_mut()
                .and_then(|call| call.remove("arguments"))
                .ok_or(invalid)?;
            let name = call["operation"]
                .as_str()
                .ok_or(Error::Invalid("tool intent binding is invalid"))?;
            let identifier = validate::identifier(&call["intent_id"])?;
            let digest = definitions
                .get(name)
                .and_then(Value::as_str)
                .filter(|digest| {
                    digest.len() == 64
                        && digest
                            .bytes()
                            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
                });
            let Some(digest) = digest.filter(|_| arguments.is_object()) else {
                return Err(Error::Invalid("tool intent binding is invalid"));
            };
            let seen = state_array(&mut state["seen_tools"])?;
            if seen.iter().any(|item| item == identifier) {
                return Err(Error::Invalid("tool intent binding is invalid"));
            }
            seen.push(json!(identifier));
            let arguments_json = String::from_utf8(canonical_bytes(&arguments, STATE_LIMIT)?)
                .map_err(|_| Error::Invalid("tool arguments are not UTF-8"))?;
            state_array(&mut state["pending_tools"])?.push(json!({
                "tool_id": name,
                "tool_call_id": identifier,
                "arguments": arguments,
                "expected_definition_hash": digest,
            }));
            calls.push(json!({
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": arguments_json},
            }));
        }
        state_array(&mut state["tool_messages"])?.push(json!({
            "role": "assistant",
            "content": if py_truthy(&output) { output } else { json!("") },
            "tool_calls": calls,
        }));
        state["stage"] = json!("tool");
    }
    Ok(Step::Intent(None))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> Value {
        json!({
            "turn_id": "turn-1",
            "conversation_id": "conversation-1",
            "conversation_revision": 1,
            "content": "こんにちは",
        })
    }

    fn owner(value: Value) -> Value {
        json!({ "status": "ok", "value": value })
    }

    fn read(mode: &str) -> Value {
        json!({
            "conversation": {
                "id": "conversation-1",
                "conversation_revision": 1,
                "messages": [],
                "model_reference": "モデル",
            },
            "delivery_mode": mode,
        })
    }

    fn append(intent: &Value, revision: i64) -> Value {
        json!({
            "action": "message_appended",
            "message": intent["payload"]["message"],
            "conversation_revision": revision,
        })
    }

    fn step(mut intent: Value, value: Value) -> Value {
        resume(intent["state"].take(), owner(value)).unwrap()
    }

    fn user_intent(request: Value) -> Value {
        step(start(request).unwrap(), read("buffered"))
    }

    #[test]
    fn complete_turn_uses_owner_revision_and_preserves_owner_message() {
        let mut initial = start(request()).unwrap();
        assert_eq!(initial["hop"], 0);
        assert_eq!(initial["state"]["user_content"], "こんにちは");
        assert!(initial["state"]["request"].get("content").is_none());
        initial["state"].as_object_mut().unwrap().remove("ai_mode");
        let user = step(initial, read("buffered"));
        assert_eq!(user["hop"], 1);
        assert_eq!(user["payload"]["message"]["content"], "こんにちは");
        assert_eq!(
            user["payload"]["message"]["id"],
            "message:fab2705432da5b411e0815a3f0aadf07b932f48f3c1f7c7605c8a82fffb913eb"
        );
        assert_eq!(user["state"]["user_content"], Value::Null);
        assert_eq!(
            user["state"]["user_content_digest"],
            "sha256:3773537041afd1d331b4de9c8794b2b803e81263f85b8ba44fe4d299dfc2549e"
        );
        let accepted = append(&user, 17);
        let ai = step(user, accepted);
        assert_eq!(ai["hop"], 2);
        assert_eq!(ai["payload"]["model_reference"], "モデル");
        let assistant = step(
            ai,
            json!({"status":"ok", "output":"回答", "finish_reason":"stop"}),
        );
        assert_eq!(assistant["hop"], 3);
        assert_eq!(assistant["payload"]["expected_conversation_revision"], 17);
        assert_eq!(
            assistant["payload"]["message"]["id"],
            "message:91a1c8e7708110691c21b210b808df759e96aefa59fd3cd90668ef86a434d773"
        );
        let mut accepted = append(&assistant, 21);
        accepted["message"]["sequence_number"] = json!(7);
        let expected = accepted["message"].clone();
        let complete = step(assistant, accepted);
        assert_eq!(complete["status"], "ok");
        assert_eq!(complete["conversation_revision"], 21);
        assert_eq!(complete["message"], expected);
    }

    #[test]
    fn read_fences_duplicate_turn_before_stale_revision() {
        let mut owned = read("buffered");
        owned["conversation"]["messages"] = json!([{
            "id": intent::message_id(&request(), "user").unwrap()
        }]);
        owned["conversation"]["conversation_revision"] = json!(3);
        let result = step(start(request()).unwrap(), owned);
        assert_eq!(result["error"]["code"], "TURN_RECONCILIATION_REQUIRED");
        assert_eq!(result["user_persistence"], "unknown");
        assert_eq!(result["assistant_persistence"], "unknown");
        assert_eq!(result["reconciliation_required"], true);
    }

    #[test]
    fn an_undispatched_next_intent_reports_current_persistence() {
        let mut user = user_intent(request());
        // Keep the state below 4 MiB but make the AI intent exceed 4 MiB when
        // the model appears in both the state and the new payload.
        user["state"]["model_reference"] = json!("m".repeat(STATE_LIMIT / 2));
        let accepted = append(&user, 2);
        let result = step(user, accepted);
        assert_eq!(result["error"]["code"], "OWNER_RESPONSE_INVALID");
        assert_eq!(result["user_persistence"], "saved");
        assert_eq!(result["assistant_persistence"], "not_written");
        assert_eq!(result["reconciliation_required"], false);

        let user = user_intent(request());
        let accepted = append(&user, 2);
        let ai = step(user, accepted);
        // The owner value fits depth 12. In the next intent the same data is
        // deeper because it is inside the append message.
        let output = (0..10).fold(json!("x"), |value, _| json!([value]));
        let result = step(ai, json!({"status":"ok", "output":output}));
        assert_eq!(result["error"]["code"], "OWNER_RESPONSE_INVALID");
        assert_eq!(result["user_persistence"], "saved");
        assert_eq!(result["assistant_persistence"], "not_written");
        assert_eq!(result["reconciliation_required"], false);
    }

    #[test]
    fn tools_are_bound_and_acknowledged_in_order() {
        let mut request = request();
        request["tool_selection"] = json!({"mode": "auto", "must_use": true});
        let user = user_intent(request);
        let accepted = append(&user, 2);
        let ai = step(user, accepted);
        let required = step(ai.clone(), json!({"status":"ok", "output":"answer"}));
        assert_eq!(required["error"]["code"], "REQUIRED_TOOL_NOT_USED");
        let digest = "a".repeat(64);
        let tool = step(
            ai,
            json!({
                "status":"ok",
                "tool_definitions":{"read":digest},
                "tool_intents":[
                    {"operation":"read", "intent_id":"first", "arguments":{"z":2,"a":1}},
                    {"operation":"read", "intent_id":"second", "arguments":{}},
                ],
            }),
        );
        assert_eq!(tool["payload"]["expected_definition_hash"], digest);
        assert_eq!(tool["payload"]["tool_call_id"], "first");
        assert_eq!(
            tool["state"]["tool_messages"][0]["tool_calls"][0]["function"]["arguments"],
            "{\"a\":1,\"z\":2}"
        );
        let wrong = step(
            tool.clone(),
            json!({"tool_id":"read","tool_call_id":"second","content":"x"}),
        );
        assert_eq!(wrong["error"]["code"], "OWNER_RESPONSE_INVALID");
        assert_eq!(wrong["reconciliation_required"], true);
        let tool = step(
            tool,
            json!({"tool_id":"read","tool_call_id":"first","content":"x"}),
        );
        assert_eq!(tool["payload"]["tool_call_id"], "second");
        let ai = step(
            tool,
            json!({"tool_id":"read","tool_call_id":"second","content":"y"}),
        );
        assert_eq!(ai["state"]["stage"], "ai");
        assert_eq!(ai["state"]["tool_count"], 2);
        let assistant = step(ai, json!({"status":"ok","output":"answer"}));
        assert_eq!(
            assistant["payload"]["message"]["tool_logs"],
            json!([
                {"tool_name":"read", "tool_call_id":"first", "arguments":{"a":1,"z":2}, "result":"x"},
                {"tool_name":"read", "tool_call_id":"second", "arguments":{}, "result":"y"},
            ])
        );
    }

    #[test]
    fn resume_rejects_a_hop_outside_the_finite_state_machine() {
        let mut state = start(request()).unwrap()["state"].take();
        state["hop"] = json!(20);
        assert_eq!(
            resume(state, owner(read("buffered"))),
            Err(Error::Invalid("saved continuation hop is invalid"))
        );
    }
}
