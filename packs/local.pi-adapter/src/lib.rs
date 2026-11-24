//! Typed adapter between a wrapped pi extension and Flow port operations.
wit_bindgen::generate!({path:"guest-wit",world:"pack",generate_all});
use exports::local::harness_agent::{tool_call_guard, tool_provider};
use exports::local::harness_chat::command_provider;
use exports::tobkiri::core::lifecycle;
use local::pi_adapter::pi_extension;
use tobkiri::core::types::{Error, ErrorCode};

struct Pack;
fn invalid(message: &str) -> Error {
    Error {
        code: ErrorCode::Invalid,
        message: message.into(),
    }
}
impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        pi_extension::load()?;
        Ok(())
    }
    fn shutdown() {}
}
impl tool_provider::Guest for Pack {
    fn invoke(call: String) -> Result<String, Error> {
        let call: serde_json::Value =
            serde_json::from_str(&call).map_err(|_| invalid("invalid tool call"))?;
        let name = call
            .get("name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| invalid("tool name required"))?;
        let id = call
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| invalid("tool call ID required"))?;
        let args = call
            .get("arguments")
            .ok_or_else(|| invalid("tool arguments required"))?
            .to_string();
        pi_extension::invoke_tool(name, id, &args)
    }
}
impl tool_call_guard::Guest for Pack {
    fn on_tool_call(call: String, has_ui: bool) -> Result<tool_call_guard::Decision, Error> {
        let value: serde_json::Value =
            serde_json::from_str(&call).map_err(|_| invalid("invalid tool call"))?;
        let event=serde_json::json!({"toolName":value.get("name"),"input":value.get("arguments"),"toolCallId":value.get("id")}).to_string();
        let decision = pi_extension::on_event("tool_call", &event, has_ui)?;
        let value = match decision {
            Some(v) => serde_json::from_str::<serde_json::Value>(&v)
                .map_err(|_| invalid("invalid pi guard decision"))?,
            None => serde_json::Value::Null,
        };
        Ok(tool_call_guard::Decision {
            block: value
                .get("block")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            reason: value
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .into(),
            input: call,
        })
    }
}
impl command_provider::Guest for Pack {
    fn invoke(name: String, args: String) -> Result<String, Error> {
        pi_extension::run_command(&name, &args)
    }
}
export!(Pack);
