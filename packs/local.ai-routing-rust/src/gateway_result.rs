use ordered_python_compat::{MAX_NODES, PyValue, dumps_bounded, loads};
use tobkiri_ai_gateway_finalizer_core::{
    CandidateIdentity, assemble_stream_result, normalize_generate_result,
};

use crate::MAX_OUTPUT_BYTES;

/// Project Gateway result data. Leave unsupported input and original errors
/// to the caller's compatibility path.
pub fn project_gateway_result(request: &str) -> Option<String> {
    if request.len() > MAX_OUTPUT_BYTES {
        return None;
    }
    let payload = loads(request).ok()?;
    let request_id = payload.get("request_id")?.as_str()?;
    let candidate = payload.get("candidate")?;
    if candidate.as_object()?.len() != 4 {
        return None;
    }
    let identity = CandidateIdentity {
        model_id: candidate.get("model_id")?.as_str()?,
        provider_instance_id: candidate.get("provider_instance_id")?.as_str()?,
        catalog_provider_instance_id: candidate.get("catalog_provider_instance_id")?.as_str()?,
        catalog_revision: candidate.get("catalog_revision")?.as_str()?,
    };
    let value = payload.get("value")?;
    let field_count = payload.as_object()?.len();
    let projected = match payload.get("role")?.as_str()? {
        "generate" if field_count == 4 => {
            // Avoid allocating an array that cannot fit the ordered codec.
            if value
                .get("tool_intents")
                .and_then(PyValue::as_str)
                .is_some_and(|text| text.chars().take(MAX_NODES).count() == MAX_NODES)
            {
                return None;
            }
            let (result, usage_has_non_string_key) =
                normalize_generate_result(value, request_id, identity).ok()?;
            if usage_has_non_string_key {
                return None;
            }
            result
        }
        "stream" if field_count == 7 => assemble_stream_result(
            value,
            request_id,
            identity,
            object_list(&payload, "tool_intents")?,
            object_list(&payload, "events")?,
            object_list(&payload, "attempts")?,
        )
        .ok()?,
        _ => return None,
    };
    dumps_bounded(&projected, MAX_OUTPUT_BYTES).ok()
}

fn object_list(payload: &PyValue, key: &str) -> Option<PyValue> {
    let value = payload.get(key)?;
    value
        .as_array()?
        .iter()
        .all(|item| item.as_object().is_some())
        .then(|| value.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    const CANDIDATE: &str = r#"{"model_id":"m","provider_instance_id":"p","catalog_provider_instance_id":"cp","catalog_revision":"r"}"#;

    fn generate(value: &str) -> Option<String> {
        project_gateway_result(&format!(
            r#"{{"role":"generate","value":{value},"request_id":"req","candidate":{CANDIDATE}}}"#
        ))
    }

    fn stream(value: &str) -> Option<String> {
        project_gateway_result(&format!(
            r#"{{"role":"stream","value":{value},"request_id":"req","candidate":{CANDIDATE},"tool_intents":[{{"id":"normalized"}}],"events":[{{"type":"usage","usage_cost":{{"z":1,"a":2}}}},{{"type":"finish"}}],"attempts":[{{"n":1}}]}}"#
        ))
    }

    #[test]
    fn generate_preserves_field_order_and_python_conversions() {
        assert_eq!(
            generate(r#"{"output":{"z":1,"a":2},"tool_intents":"é😀","finish_reason":false,"usage":[["z",1],"ab",["z",3],{"y":1,"x":2}],"usage_provenance":true}"#)
                .as_deref(),
            Some(r#"{"status":"ok","request_id":"req","model_id":"m","provider_instance_id":"p","catalog_provider_instance_id":"cp","catalog_revision":"r","pricing_revision":"r","output":{"z":1,"a":2},"tool_intents":["é","😀"],"finish_reason":"stop","usage":{"z":3,"a":"b","y":"x"},"usage_provenance":"True"}"#),
        );
        assert_eq!(
            generate(r#"{"status":null}"#).as_deref(),
            Some(
                r#"{"status":"ok","request_id":"req","model_id":"m","provider_instance_id":"p","catalog_provider_instance_id":"cp","catalog_revision":"r","pricing_revision":"r","output":null,"tool_intents":[],"finish_reason":"stop","usage":{},"usage_provenance":"provider_reported"}"#
            ),
        );
    }

    #[test]
    fn generate_preserves_optional_task_evidence_and_priority() {
        for (value, expected) in [
            (r#"{}"#, None),
            (r#"{"metadata":[]}"#, None),
            (r#"{"metadata":{}}"#, Some("unknown")),
            (
                r#"{"finish_reason":"STOP","metadata":{}}"#,
                Some("completed"),
            ),
            (
                r#"{"finish_reason":"stop","metadata":{"task_state":"running"},"task_state":null}"#,
                Some("completed"),
            ),
            (
                r#"{"finish_reason":"stop","metadata":{"thinking":{"state":"idle"}}}"#,
                Some("unknown"),
            ),
            (
                r#"{"finish_reason":"stop","task_state":false}"#,
                Some("unknown"),
            ),
            (
                r#"{"finish_reason":"other","task_state":null}"#,
                Some("failed"),
            ),
            (
                r#"{"finish_reason":"error","metadata":{"waiting_for_user":true}}"#,
                Some("waiting_user"),
            ),
            (
                r#"{"finish_reason":"tool_calls","metadata":{"waiting_for_user":true},"task_state":"awaiting_approval"}"#,
                Some("waiting_approval"),
            ),
            (
                r#"{"finish_reason":"cancelled","metadata":{"approval_required":true}}"#,
                Some("cancelled"),
            ),
            (
                r#"{"finish_reason":"tool_calls","metadata":{"cancelled":1}}"#,
                Some("running"),
            ),
        ] {
            let projected = loads(&generate(value).unwrap()).unwrap();
            assert_eq!(
                projected.get("task_state").and_then(PyValue::as_str),
                expected,
                "{value}"
            );
            assert!(projected.get("metadata").is_none());
        }
    }

    #[test]
    fn stream_uses_signed_results_in_original_order_and_distinguishes_null_from_missing() {
        assert_eq!(
            stream("{}").as_deref(),
            Some(
                r#"{"status":"ok","output":"","tool_intents":[{"id":"normalized"}],"finish_reason":null,"delivery_mode":"buffered","request_id":"req","model_id":"m","provider_instance_id":"p","events":[{"type":"usage","usage_cost":{"z":1,"a":2}},{"type":"finish"}],"attempts":[{"n":1}]}"#
            ),
        );
        assert_eq!(
            stream(r#"{"output":null,"finish_reason":false,"delivery_mode":null,"tool_intents":["ignored"],"events":["ignored"],"attempts":["ignored"]}"#).as_deref(),
            Some(r#"{"status":"ok","output":null,"tool_intents":[{"id":"normalized"}],"finish_reason":false,"delivery_mode":null,"request_id":"req","model_id":"m","provider_instance_id":"p","events":[{"type":"usage","usage_cost":{"z":1,"a":2}},{"type":"finish"}],"attempts":[{"n":1}]}"#),
        );
    }

    #[test]
    fn errors_malformed_values_and_resource_overflow_defer_to_gateway() {
        for value in [
            "[]",
            r#"{"status":"error","error_code":"quota"}"#,
            r#"{"status":[]}"#,
            r#"{"tool_intents":2}"#,
            r#"{"usage":[[1,2]]}"#,
            r#"{"usage":[[["unhashable"],2]]}"#,
            r#"{"usage":[["wrong length"]]}"#,
            r#"{"output":1e1000}"#,
        ] {
            assert!(generate(value).is_none(), "{value}");
        }
        assert!(stream("[]").is_none());
        for payload in [
            r#"{"role":"unknown","request_id":"req","candidate":CANDIDATE,"value":{}}"#,
            r#"{"role":"generate","request_id":null,"candidate":CANDIDATE,"value":{}}"#,
            r#"{"role":"generate","request_id":"req","candidate":{},"value":{}}"#,
            r#"{"role":"generate","request_id":"req","candidate":CANDIDATE}"#,
            r#"{"role":"generate","request_id":"req","candidate":CANDIDATE,"value":{},"extra":true}"#,
            r#"{"role":"stream","request_id":"req","candidate":CANDIDATE,"value":{},"tool_intents":[],"events":[]}"#,
            r#"{"role":"stream","request_id":"req","candidate":CANDIDATE,"value":{},"tool_intents":[],"events":[null],"attempts":[]}"#,
        ] {
            assert!(
                project_gateway_result(&payload.replace("CANDIDATE", CANDIDATE)).is_none(),
                "{payload}"
            );
        }
        let message = "x".repeat(MAX_OUTPUT_BYTES);
        assert!(generate(&format!(r#"{{"output":"{message}"}}"#)).is_none());
        let expanding = "x".repeat(MAX_NODES);
        assert!(generate(&format!(r#"{{"tool_intents":"{expanding}"}}"#)).is_none());
        let duplicated_revision = "x".repeat(MAX_OUTPUT_BYTES / 2);
        let payload = format!(
            r#"{{"role":"generate","request_id":"req","candidate":{{"model_id":"m","provider_instance_id":"p","catalog_provider_instance_id":"cp","catalog_revision":"{duplicated_revision}"}},"value":{{}}}}"#
        );
        assert!(payload.len() < MAX_OUTPUT_BYTES);
        assert!(project_gateway_result(&payload).is_none());
    }
}
