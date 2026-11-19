use std::collections::BTreeMap;

use serde_json::{Value, json};
use tobkiri_canon::Digest;

use crate::validate::{is_digest, saved_user_content};
use crate::{Error, Result, STATE_LIMIT, canonical_bytes, check_canonical_size, py_truthy};

const CONVERSATION_RESOURCE: (&str, &str) = (
    "tobkiri.resource.conversation.v1",
    "rumi_conversation_store_pack.conversation-resource",
);
const MESSAGE_MANAGE: (&str, &str) = (
    "tobkiri.action.message.manage.v1",
    "rumi_conversation_store_pack.message-manage",
);
const AI_GENERATE: (&str, &str) = (
    "tobkiri.service.ai.generate.v1",
    "rumi_ai_gateway_pack.ai-gateway.generate",
);
const AI_STREAM: (&str, &str) = (
    "tobkiri.service.ai.stream.v1",
    "rumi_ai_gateway_pack.ai-gateway.stream",
);
const AI_STRATEGY: (&str, &str) = (
    "tobkiri.service.ai.strategy.dispatch.v1",
    "rumi_ai_strategy_runtime_pack.ai-strategy.dispatch",
);
const TOOL_INVOKE: (&str, &str) = (
    "tobkiri.service.tool.invoke.v1",
    "rumi_tool_broker_pack.tool-invoke",
);

fn required<'a>(value: &'a Value, key: &str) -> Result<&'a Value> {
    value
        .get(key)
        .ok_or(Error::Invalid("saved continuation fields are invalid"))
}

/// Derive the same stable message identity for an append and its acknowledgement.
pub(crate) fn message_id(request: &Value, role: &str) -> Result<String> {
    let identity = json!([
        required(request, "conversation_id")?,
        required(request, "turn_id")?,
        role,
    ]);
    let digest = Digest::hash(&canonical_bytes(&identity, STATE_LIMIT)?);
    Ok(format!("message:{}", digest.hex()))
}

fn tool_logs(messages: &Value) -> Result<Vec<Value>> {
    let invalid = || Error::Invalid("saved tool history is invalid");
    let mut calls = BTreeMap::new();
    let mut logs = Vec::new();
    for message in messages.as_array().ok_or_else(invalid)? {
        if required(message, "role")? == "assistant" {
            for item in required(message, "tool_calls")?
                .as_array()
                .ok_or_else(invalid)?
            {
                let id = required(item, "id")?.as_str().ok_or_else(invalid)?;
                calls.insert(id, required(item, "function")?);
            }
        } else {
            let id = required(message, "tool_call_id")?
                .as_str()
                .ok_or_else(invalid)?;
            let call = calls.get(id).ok_or_else(invalid)?;
            let arguments = required(call, "arguments")?.as_str().ok_or_else(invalid)?;
            let arguments: Value = serde_json::from_str(arguments).map_err(|_| invalid())?;
            let mut log = json!({
                "tool_name": required(call, "name")?,
                "tool_call_id": id,
                "result": required(message, "content")?,
            });
            log["arguments"] = arguments;
            logs.push(log);
        }
    }
    Ok(logs)
}

/// Persist the turn identity, owner-resolved references and approval mode.
pub(crate) fn message_metadata(request: &Value) -> Result<Value> {
    let mut metadata = json!({"turn_id": required(request, "turn_id")?});
    if let Some(resolved) = request.get("resolved_chat_references") {
        let references = required(resolved, "references")?;
        check_canonical_size(references, STATE_LIMIT)?;
        metadata["chat_references"] = references.clone();
    }
    if let Some(mode) = request.get("action_approval_mode") {
        metadata["action_approval_mode"] = mode.clone();
    }
    Ok(metadata)
}

/// Reconstruct the message for an append or acknowledgement comparison.
pub(crate) fn message(state: &Value, role: &str, user_content: Option<&Value>) -> Result<Value> {
    message_with_content(state, role, user_content.cloned())
}

fn message_with_content(state: &Value, role: &str, user_content: Option<Value>) -> Result<Value> {
    let request = required(state, "request")?;
    let assistant = required(state, "assistant")?;
    let (content, parent_id) = if role == "user" {
        let content = match user_content {
            Some(value) if !value.is_null() => value,
            _ => required(state, "user_content")?.clone(),
        };
        if !saved_user_content(&content) {
            return Err(Error::Invalid(
                "saved conversation user content is unavailable",
            ));
        }
        (content, required(state, "parent_id")?.clone())
    } else {
        (
            assistant.clone(),
            Value::String(message_id(request, "user")?),
        )
    };
    let mut metadata = message_metadata(request)?;
    let mut result = json!({
        "id": message_id(request, role)?,
        "role": role,
        "parent_id": parent_id,
        "status": "complete",
    });
    if role == "assistant" {
        result["finish_reason"] = required(state, "assistant_finish_reason")?.clone();
        let messages = required(state, "tool_messages")?;
        if py_truthy(messages) {
            metadata["saved_tool_messages"] = messages.clone();
            result["tool_logs"] = Value::Array(tool_logs(messages)?);
        }
    }
    result["content"] = content;
    result["metadata"] = metadata;
    Ok(result)
}

/// Build a bounded continuation intent for the state's next owner operation.
pub(crate) fn intent(state: Value, user_content: Option<Value>) -> Result<Value> {
    let stage = required(&state, "stage")?
        .as_str()
        .ok_or(Error::Invalid("saved continuation hop is invalid"))?;
    let request = required(&state, "request")?;
    let prompt_digest = required(&state, "system_prompt_digest")?;
    if !prompt_digest.is_null() && !prompt_digest.as_str().is_some_and(is_digest) {
        return Err(Error::Invalid("saved system prompt binding is invalid"));
    }

    let (target, payload) = match stage {
        "read" => (
            CONVERSATION_RESOURCE,
            json!({"operation": "get", "conversation_id": required(request, "conversation_id")?}),
        ),
        "user" | "assistant" => {
            let mut payload = json!({
                "operation": "append",
                "conversation_id": required(request, "conversation_id")?,
                "expected_conversation_revision": required(&state, "revision")?,
            });
            payload["message"] = message_with_content(&state, stage, user_content)?;
            if stage == "user" && !prompt_digest.is_null() {
                payload["system_prompt_digest"] = prompt_digest.clone();
            }
            (MESSAGE_MANAGE, payload)
        }
        "tool" => (
            TOOL_INVOKE,
            required(&state, "pending_tools")?
                .get(0)
                .ok_or(Error::Invalid("saved tool intent is unavailable"))?
                .clone(),
        ),
        "ai" => {
            let mut payload = json!({
                "model_reference": required(&state, "model_reference")?,
                "requirements": {"request_surface": "conversation.saved"},
            });
            if !prompt_digest.is_null() {
                payload["system_prompt_digest"] = prompt_digest.clone();
            }
            if let Some(strategy) = request
                .get("strategy_reference")
                .filter(|value| !value.is_null())
            {
                let mut dispatch = json!({"strategy_reference": strategy});
                dispatch["request"] = payload;
                (AI_STRATEGY, dispatch)
            } else {
                let target = if required(&state, "ai_mode")? == "incremental" {
                    AI_STREAM
                } else {
                    AI_GENERATE
                };
                (target, payload)
            }
        }
        _ => return Err(Error::Invalid("saved continuation hop is invalid")),
    };

    let mut value = json!({
        "kind": "tobkiri.packvm.continuation.intent.v2",
        "hop": required(&state, "hop")?,
        "target": {"contract_id": target.0, "operation_id": target.1},
    });
    value["payload"] = payload;
    value["state"] = state;
    check_canonical_size(&value, STATE_LIMIT)?;
    Ok(value)
}

/// Report persistence according to the stage that was actually dispatched.
pub(crate) fn failure(state: &Value, stage: &str, code: &str) -> Result<Value> {
    let request = required(state, "request")?;
    let mut user = match stage {
        "read" => "not_written",
        "user" => "unknown",
        _ => "saved",
    };
    let mut assistant = if stage == "assistant" {
        "unknown"
    } else {
        "not_written"
    };
    let mut reconcile = if matches!(stage, "user" | "assistant" | "tool") {
        true
    } else {
        match required(state, "tool_count")? {
            Value::Bool(count) => *count,
            Value::Number(count) => count.as_i64().is_some_and(|count| count > 0),
            _ => return Err(Error::Invalid("saved tool count is invalid")),
        }
    };
    if code == "TURN_RECONCILIATION_REQUIRED" {
        user = "unknown";
        assistant = "unknown";
        reconcile = true;
    }

    Ok(json!({
        "status": "error",
        "error": {"code": code, "message": "Saved conversation did not complete."},
        "turn_id": required(request, "turn_id")?,
        "conversation_id": required(request, "conversation_id")?,
        "user_message_id": message_id(request, "user")?,
        "assistant_message_id": message_id(request, "assistant")?,
        "user_persistence": user,
        "assistant_persistence": assistant,
        "reconciliation_required": reconcile,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(stage: &str) -> Value {
        json!({
            "hop": 2,
            "request": {
                "turn_id": "turn-1",
                "conversation_id": "conv-1",
                "conversation_revision": 1,
                "action_approval_mode": "ask",
                "resolved_chat_references": {"references": [{"title": "参考"}]},
            },
            "model_reference": "model",
            "revision": 3,
            "parent_id": "prior",
            "assistant": "stored answer",
            "assistant_finish_reason": "stop",
            "stage": stage,
            "pending_tools": [],
            "tool_messages": [],
            "tool_count": 0,
            "seen_tools": [],
            "system_prompt_digest": null,
            "user_content": null,
            "user_content_digest": null,
            "ai_mode": "buffered",
        })
    }

    #[test]
    fn read_and_append_use_python_message_ids_and_metadata() {
        let initial = state("read");
        let read = intent(initial.clone(), None).unwrap();
        assert_eq!(
            read,
            json!({
                "kind": "tobkiri.packvm.continuation.intent.v2",
                "hop": 2,
                "target": {
                    "contract_id": "tobkiri.resource.conversation.v1",
                    "operation_id": "rumi_conversation_store_pack.conversation-resource",
                },
                "payload": {"operation": "get", "conversation_id": "conv-1"},
                "state": initial,
            })
        );

        let mut user = state("user");
        user["system_prompt_digest"] = json!(format!("sha256:{}", "a".repeat(64)));
        let append = intent(user.clone(), Some(json!("ユーザー\t\0é"))).unwrap();
        assert_eq!(
            append["target"]["contract_id"],
            "tobkiri.action.message.manage.v1"
        );
        assert_eq!(
            append["target"]["operation_id"],
            "rumi_conversation_store_pack.message-manage"
        );
        assert_eq!(append["state"], user);
        assert_eq!(
            append["payload"],
            json!({
                "operation": "append",
                "conversation_id": "conv-1",
                "expected_conversation_revision": 3,
                "system_prompt_digest": user["system_prompt_digest"],
                "message": {
                    "id": "message:fa9e859a2ec4720c333852dfc325f86cd30c2a132a5d4d77e226a5bda3cfea36",
                    "role": "user",
                    "content": "ユーザー\t\0é",
                    "parent_id": "prior",
                    "metadata": {
                        "turn_id": "turn-1",
                        "chat_references": [{"title": "参考"}],
                        "action_approval_mode": "ask",
                    },
                    "status": "complete",
                },
            })
        );
        assert_eq!(
            Digest::hash(&canonical_bytes(&append, STATE_LIMIT).unwrap()).hex(),
            "87decea3da797075fb0c8912f5051dac7c3ff9d46807a78c8049fe42d385fd95"
        );
    }

    #[test]
    fn ai_delivery_selects_gateway_or_strategy_and_preserves_prompt_binding() {
        let mut ai = state("ai");
        ai["system_prompt_digest"] = json!(format!("sha256:{}", "b".repeat(64)));
        let expected = json!({
            "model_reference": "model",
            "requirements": {"request_surface": "conversation.saved"},
            "system_prompt_digest": ai["system_prompt_digest"],
        });
        let buffered = intent(ai.clone(), None).unwrap();
        assert_eq!(
            buffered["target"],
            json!({
                "contract_id": "tobkiri.service.ai.generate.v1",
                "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
            })
        );
        assert_eq!(buffered["payload"], expected);

        ai["ai_mode"] = json!("incremental");
        ai["request"]["strategy_reference"] = Value::Null;
        let stream = intent(ai.clone(), None).unwrap();
        assert_eq!(
            stream["target"],
            json!({
                "contract_id": "tobkiri.service.ai.stream.v1",
                "operation_id": "rumi_ai_gateway_pack.ai-gateway.stream",
            })
        );
        assert_eq!(stream["payload"], expected);

        ai["request"]["strategy_reference"] = json!("strategy-1");
        let strategy = intent(ai, None).unwrap();
        assert_eq!(
            strategy["target"],
            json!({
                "contract_id": "tobkiri.service.ai.strategy.dispatch.v1",
                "operation_id": "rumi_ai_strategy_runtime_pack.ai-strategy.dispatch",
            })
        );
        assert_eq!(
            strategy["payload"],
            json!({
                "strategy_reference": "strategy-1", "request": expected,
            })
        );
    }

    #[test]
    fn read_intent_rejects_invalid_prompt_binding_even_when_it_is_absent_from_payload() {
        let mut read = state("read");
        read["system_prompt_digest"] = json!(format!("sha256:{}", "A".repeat(64)));
        assert_eq!(
            intent(read, None),
            Err(Error::Invalid("saved system prompt binding is invalid"))
        );
    }

    #[test]
    fn tool_intent_and_append_keep_completed_tool_trace_and_log_order() {
        let mut tool = state("tool");
        let pending = json!({
            "tool_id": "lookup", "tool_call_id": "second", "arguments": {"q": "雪"},
            "expected_definition_hash": "a".repeat(64),
        });
        tool["pending_tools"] = json!([pending, {"tool_call_id": "first"}]);
        let invocation = intent(tool, None).unwrap();
        assert_eq!(
            invocation["target"],
            json!({
                "contract_id": "tobkiri.service.tool.invoke.v1",
                "operation_id": "rumi_tool_broker_pack.tool-invoke",
            })
        );
        assert_eq!(invocation["payload"], pending);

        let mut assistant = state("assistant");
        let trace = json!([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "first", "type": "function", "function": {
                    "name": "status", "arguments": "{}",
                }},
                {"id": "second", "type": "function", "function": {
                    "name": "lookup", "arguments": "{\"q\":\"雪\"}",
                }},
            ]},
            {"role": "tool", "tool_call_id": "second", "content": "lookup result"},
            {"role": "tool", "tool_call_id": "first", "content": "status result"},
        ]);
        assistant["tool_messages"] = trace.clone();
        assistant["system_prompt_digest"] = json!(format!("sha256:{}", "c".repeat(64)));
        let append = intent(assistant.clone(), None).unwrap();
        let persisted = message(&assistant, "assistant", None).unwrap();
        assert_eq!(append["payload"]["message"], persisted);
        assert!(append["payload"].get("system_prompt_digest").is_none());
        assert_eq!(
            persisted["id"],
            "message:98620211c1c889cfe3b8e5213d86af4b31769ce779c6243a19131087a7bde97b"
        );
        assert_eq!(
            persisted["parent_id"],
            "message:fa9e859a2ec4720c333852dfc325f86cd30c2a132a5d4d77e226a5bda3cfea36"
        );
        assert_eq!(persisted["finish_reason"], "stop");
        assert_eq!(persisted["metadata"]["saved_tool_messages"], trace);
        assert_eq!(
            persisted["tool_logs"],
            json!([
                {"tool_name": "lookup", "tool_call_id": "second", "arguments": {"q": "雪"}, "result": "lookup result"},
                {"tool_name": "status", "tool_call_id": "first", "arguments": {}, "result": "status result"},
            ])
        );
    }

    #[test]
    fn failure_tracks_dispatch_stage_and_reconciles_duplicate_turns() {
        for (stage, tools, code, user, assistant, reconcile) in [
            (
                "read",
                0,
                "HOST_ACTION_FAILED",
                "not_written",
                "not_written",
                false,
            ),
            (
                "user",
                0,
                "HOST_ACTION_FAILED",
                "unknown",
                "not_written",
                true,
            ),
            (
                "ai",
                0,
                "OWNER_RESPONSE_INVALID",
                "saved",
                "not_written",
                false,
            ),
            (
                "ai",
                1,
                "OWNER_RESPONSE_INVALID",
                "saved",
                "not_written",
                true,
            ),
            (
                "tool",
                0,
                "HOST_ACTION_FAILED",
                "saved",
                "not_written",
                true,
            ),
            (
                "assistant",
                0,
                "HOST_ACTION_FAILED",
                "saved",
                "unknown",
                true,
            ),
            (
                "read",
                0,
                "TURN_RECONCILIATION_REQUIRED",
                "unknown",
                "unknown",
                true,
            ),
        ] {
            let mut failed = state("ai");
            failed["tool_count"] = json!(tools);
            assert_eq!(
                failure(&failed, stage, code).unwrap(),
                json!({
                    "status": "error",
                    "error": {"code": code, "message": "Saved conversation did not complete."},
                    "turn_id": "turn-1",
                    "conversation_id": "conv-1",
                    "user_message_id": "message:fa9e859a2ec4720c333852dfc325f86cd30c2a132a5d4d77e226a5bda3cfea36",
                    "assistant_message_id": "message:98620211c1c889cfe3b8e5213d86af4b31769ce779c6243a19131087a7bde97b",
                    "user_persistence": user,
                    "assistant_persistence": assistant,
                    "reconciliation_required": reconcile,
                })
            );
        }
    }
}
