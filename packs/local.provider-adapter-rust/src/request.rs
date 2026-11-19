use ordered_python_compat::{MAX_BYTES, MAX_NODES, PyValue, dumps_bounded, loads, pystr, pytruthy};
use provider_model_access::compile_connection_parameters;
use std::collections::BTreeMap;

const MAX_REQUEST_BYTES: usize = 1024 * 1024;
const MAX_SOURCE_DEPTH: usize = 256;

/// Compile the finite loopback chat body with the local Python adapter's ordering.
/// `None` lets the caller use its original compiler before sending a request.
pub fn compile_local_chat_request(source: &str) -> Option<String> {
    RequestSource::parse(source)?.compile_body()
}

/// Apply the selected local Registry record's effective policy and serialize the body.
/// `Ok(None)` leaves unsupported representations to the original policy and codec.
pub fn compile_local_chat_request_with_policy(
    source: &str,
    policy: &str,
) -> Result<Option<String>, PolicyError> {
    let Some(mut request) = RequestSource::parse(source) else {
        return Ok(None);
    };
    let connection = if policy.is_empty() {
        PyValue::Object(Vec::new())
    } else {
        let Some(policy) = load_source(policy).filter(|value| value.as_object().is_some()) else {
            return Ok(None);
        };
        // The caller selects a local Registry record and passes its effective policy.
        // No local connection can qualify for official OpenRouter native filters.
        PyValue::Object(vec![("model_access".into(), policy)])
    };

    match compile_connection_parameters(&connection, &request.model, &request.parameters) {
        Ok(parameters) => request.parameters = parameters,
        Err(error) => {
            return match PolicyError::from_source(error.exception_class, &error.message) {
                Some(error) => Err(error),
                None => Ok(None),
            };
        }
    }
    Ok(request.compile_body())
}

/// Policy failures with a fixed message safe to return across the component boundary.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PolicyError {
    Denied,
    InvalidModel,
    RouteOverride,
}

impl PolicyError {
    const DENIED: &str = "model is not permitted by this connection";
    const INVALID_MODEL: &str = "provider model ID is invalid";
    const ROUTE_OVERRIDE: &str = "request parameters cannot override the saved model route";

    fn from_source(exception_class: &str, message: &str) -> Option<Self> {
        match (exception_class, message) {
            ("PermissionError", Self::DENIED) => Some(Self::Denied),
            ("ValueError", Self::INVALID_MODEL) => Some(Self::InvalidModel),
            ("ValueError", Self::ROUTE_OVERRIDE) => Some(Self::RouteOverride),
            _ => None,
        }
    }

    /// The original public message for this fixed failure.
    pub fn message(self) -> &'static str {
        match self {
            Self::Denied => Self::DENIED,
            Self::InvalidModel => Self::INVALID_MODEL,
            Self::RouteOverride => Self::ROUTE_OVERRIDE,
        }
    }
}

struct RequestSource {
    model: PyValue,
    messages: PyValue,
    parameters: PyValue,
    tools: PyValue,
}

impl RequestSource {
    fn parse(source: &str) -> Option<Self> {
        let [
            mut model_id,
            mut provider_id,
            mut messages,
            mut parameters,
            mut tools,
        ] = [const { PyValue::Null }; 5];
        for (key, value) in load_source(source)?.into_object()? {
            let field = match key.as_str() {
                "model_id" => &mut model_id,
                "provider_id" => &mut provider_id,
                "messages" => &mut messages,
                "parameters" => &mut parameters,
                "tools" => &mut tools,
                _ => return None,
            };
            *field = value;
        }

        let mut model = model_string(&model_id);
        let provider_id = model_string(&provider_id);
        if !provider_id.is_empty()
            && let Some(suffix) = model
                .strip_prefix(&provider_id)
                .and_then(|suffix| suffix.strip_prefix('/'))
        {
            model = suffix.to_owned();
        }
        Some(Self {
            model: PyValue::String(model),
            messages,
            parameters,
            tools,
        })
    }

    fn compile_body(self) -> Option<String> {
        let Self {
            model,
            messages,
            parameters,
            tools,
        } = self;
        let messages = if pytruthy(&messages) {
            iterable(messages)?
        } else {
            Vec::new()
        };
        let mut entries = vec![
            ("model".to_owned(), model),
            ("messages".to_owned(), PyValue::Array(messages)),
            ("stream".to_owned(), PyValue::Bool(false)),
        ];
        let mut positions: BTreeMap<_, _> = entries
            .iter()
            .enumerate()
            .map(|(index, (key, _))| (key.clone(), index))
            .collect();
        for (key, value) in parameter_entries(parameters)? {
            if let Some(index) = positions.get(&key).copied() {
                entries[index].1 = value;
            } else {
                positions.insert(key.clone(), entries.len());
                entries.push((key, value));
            }
        }
        if tools.as_array().is_some_and(|items| !items.is_empty()) {
            if let Some(index) = positions.get("tools").copied() {
                entries[index].1 = tools;
            } else {
                entries.push(("tools".to_owned(), tools));
            }
        }

        dumps_bounded(&PyValue::Object(entries), MAX_REQUEST_BYTES).ok()
    }
}

fn load_source(source: &str) -> Option<PyValue> {
    if source.len() > MAX_BYTES || !supported_depth(source) {
        return None;
    }
    loads(source).ok()
}

fn model_string(value: &PyValue) -> String {
    if pytruthy(value) {
        pystr(value)
    } else {
        String::new()
    }
}

fn iterable(value: PyValue) -> Option<Vec<PyValue>> {
    match value {
        value @ PyValue::Array(_) => value.into_array(),
        value @ PyValue::Object(_) => Some(
            value
                .into_object()?
                .into_iter()
                .map(|(key, _)| PyValue::String(key))
                .collect(),
        ),
        PyValue::String(ref text) => {
            if text.chars().take(MAX_NODES + 1).count() > MAX_NODES {
                return None;
            }
            Some(
                text.chars()
                    .map(|character| PyValue::String(character.to_string()))
                    .collect(),
            )
        }
        _ => None,
    }
}

fn parameter_entries(value: PyValue) -> Option<Vec<(String, PyValue)>> {
    if !pytruthy(&value) {
        return Some(Vec::new());
    }
    match value {
        value @ PyValue::Object(_) => value.into_object(),
        value @ PyValue::Array(_) => value
            .into_array()?
            .into_iter()
            .map(|pair| {
                let [key, value]: [PyValue; 2] = iterable(pair)?.try_into().ok()?;
                // Python accepts other hashable keys, some of which collide
                // after JSON encoding. Leave those keys to the original path.
                Some((key.into_string()?, value))
            })
            .collect(),
        _ => None,
    }
}

fn supported_depth(source: &str) -> bool {
    let mut bytes = source.bytes();
    let mut quoted = false;
    let mut depth: usize = 0;
    while let Some(byte) = bytes.next() {
        match byte {
            b'\\' if quoted => {
                bytes.next();
            }
            b'"' => quoted = !quoted,
            b'[' | b'{' if !quoted => {
                depth += 1;
                if depth > MAX_SOURCE_DEPTH {
                    return false;
                }
            }
            b']' | b'}' if !quoted => {
                if depth == 0 {
                    return false;
                }
                depth -= 1;
            }
            _ => {}
        }
    }
    depth == 0 && !quoted
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected documents were generated by the original body builder and
    // json.dumps(body, ensure_ascii=False, separators=(",", ":")) in Python.
    #[test]
    fn preserves_parameter_order_and_replaces_tools_in_place() {
        let source = r#"{"model_id":"local/lab/model","provider_id":"local","messages":[{"role":"user","content":"東京"}],"parameters":{"z":1e-7,"messages":{"custom":true},"stream":true,"tools":"older","model":77,"a":-0.0},"tools":[{"type":"function","function":{"name":"été","parameters":{"large":9007199254740995}}}]}"#;
        let expected = r#"{"model":77,"messages":{"custom":true},"stream":true,"z":1e-07,"tools":[{"type":"function","function":{"name":"été","parameters":{"large":9007199254740995}}}],"a":-0.0}"#;
        assert_eq!(
            compile_local_chat_request(source).as_deref(),
            Some(expected)
        );
    }

    #[test]
    fn strips_one_exact_provider_prefix_and_appends_new_tools() {
        let source = r#"{"model_id":"ll/α/ll/α/model","provider_id":"ll/α","messages":{"z":1,"a":2},"parameters":{"temperature":1e16},"tools":["工具"]}"#;
        let expected = r#"{"model":"ll/α/model","messages":["z","a"],"stream":false,"temperature":1e+16,"tools":["工具"]}"#;
        assert_eq!(
            compile_local_chat_request(source).as_deref(),
            Some(expected)
        );
        let source = r#"{"model_id":"locality/model","provider_id":"local","messages":"文🦀","tools":{"ignored":true}}"#;
        let expected = r#"{"model":"locality/model","messages":["文","🦀"],"stream":false}"#;
        assert_eq!(
            compile_local_chat_request(source).as_deref(),
            Some(expected)
        );
    }

    #[test]
    fn follows_python_coercion_defaults_and_iterable_dict_order() {
        let source = r#"{"model_id":{"model":["ブ",true]},"provider_id":false,"messages":0,"parameters":[["tools",{"z":1}],"xy",{"temperature":0,"hot":0},["x","updated"]],"tools":[]}"#;
        let expected = r#"{"model":"{'model': ['ブ', True]}","messages":[],"stream":false,"tools":{"z":1},"x":"updated","temperature":"hot"}"#;
        assert_eq!(
            compile_local_chat_request(source).as_deref(),
            Some(expected)
        );
        let expected = r#"{"model":"","messages":[],"stream":false}"#;
        assert_eq!(compile_local_chat_request("{}").as_deref(), Some(expected));
    }

    const EXPLICIT_POLICY: &str = r#"{"version":"tobkiri.connection-model-access.v1","mode":"explicit","model_ids":["local/lab/model"]}"#;
    const ALL_POLICY: &str =
        r#"{"version":"tobkiri.connection-model-access.v1","mode":"all","model_ids":[]}"#;

    #[test]
    fn applies_policy_to_exactly_the_serialized_model_and_keeps_body_order() {
        let source = r#"{"model_id":"local/local/lab/model","provider_id":"local","messages":[{"role":"user","content":"東京"}],"parameters":{"z":1e-7,"tools":"old","stream":true,"a":-0.0},"tools":["工具"]}"#;
        let expected = r#"{"model":"local/lab/model","messages":[{"role":"user","content":"東京"}],"stream":true,"z":1e-07,"tools":["工具"],"a":-0.0}"#;
        for policy in [EXPLICIT_POLICY, ALL_POLICY, ""] {
            assert_eq!(
                compile_local_chat_request_with_policy(source, policy)
                    .unwrap()
                    .as_deref(),
                Some(expected),
            );
        }

        let source = r#"{"model_id":318,"parameters":null}"#;
        let expected = r#"{"model":"318","messages":[],"stream":false}"#;
        assert_eq!(
            compile_local_chat_request_with_policy(source, "")
                .unwrap()
                .as_deref(),
            Some(expected),
        );
    }

    #[test]
    fn returns_typed_failures_before_body_serialization() {
        let source = r#"{"model_id":"local/lab/other","provider_id":"local","messages":true,"parameters":{"endpoint":"override"}}"#;
        assert_eq!(
            compile_local_chat_request_with_policy(source, EXPLICIT_POLICY),
            Err(PolicyError::Denied),
        );
        let empty_explicit =
            r#"{"version":"tobkiri.connection-model-access.v1","mode":"explicit","model_ids":[]}"#;
        assert_eq!(
            compile_local_chat_request_with_policy(source, empty_explicit),
            Err(PolicyError::Denied),
        );
        assert_eq!(
            compile_local_chat_request_with_policy(source, ""),
            Err(PolicyError::RouteOverride),
        );
        let source = r#"{"model_id":"local/","provider_id":"local","messages":true,"parameters":{"model":7}}"#;
        assert_eq!(
            compile_local_chat_request_with_policy(source, EXPLICIT_POLICY),
            Err(PolicyError::InvalidModel),
        );
        let source = r#"{"model_id":"model","parameters":[["temperature",1]]}"#;
        assert_eq!(
            compile_local_chat_request_with_policy(source, ALL_POLICY),
            Err(PolicyError::RouteOverride),
        );
    }

    #[test]
    fn declines_unsupported_representations_and_unknown_policy_failures() {
        let source = r#"{"model_id":"model"}"#;
        for policy in [
            "{",
            "null",
            "{}",
            r#"{"version":"tobkiri.connection-model-access.v1","mode":"all","native_filters":{}}"#,
        ] {
            assert_eq!(
                compile_local_chat_request_with_policy(source, policy),
                Ok(None)
            );
        }
        for source in [
            r#"{"model_id":"model","messages":true}"#,
            r#"{"model_id":"model","messages":["\ud800"]}"#,
            r#"{"model_id":"model","parameters":{"temperature":NaN}}"#,
        ] {
            assert_eq!(
                compile_local_chat_request_with_policy(source, ALL_POLICY),
                Ok(None)
            );
        }
        let source = format!(
            r#"{{"model_id":"model","messages":["{}"]}}"#,
            "a".repeat(MAX_REQUEST_BYTES)
        );
        assert_eq!(
            compile_local_chat_request_with_policy(&source, ALL_POLICY),
            Ok(None)
        );
    }

    #[test]
    fn leaves_inputs_outside_the_compiler_domain_to_python() {
        for source in [
            r#"[]"#,
            r#"{"endpoint":"loopback"}"#,
            r#"{"parameters":{"temperature":NaN}}"#,
            r#"{"parameters":[[1,2]]}"#,
            r#"{"parameters":{"model":1,"model":2}}"#,
            r#"{"messages":true}"#,
            r#"{"messages":["\ud800"]}"#,
        ] {
            assert_eq!(compile_local_chat_request(source), None, "{source}");
        }
        let source = format!(
            r#"{{"messages":{}null{}}}"#,
            "[".repeat(MAX_SOURCE_DEPTH),
            "]".repeat(MAX_SOURCE_DEPTH),
        );
        assert_eq!(compile_local_chat_request(&source), None);
    }

    #[test]
    fn counts_utf8_bytes_at_the_legacy_request_limit() {
        let empty = r#"{"model":"","messages":[],"stream":false}"#;
        let bytes = MAX_REQUEST_BYTES - empty.len();
        let mut model = "é".repeat(bytes / 2);
        if bytes % 2 != 0 {
            model.push('x');
        }
        let source = format!(r#"{{"model_id":"{model}"}}"#);
        assert_eq!(
            compile_local_chat_request(&source).map(|body| body.len()),
            Some(MAX_REQUEST_BYTES)
        );
        let source = format!(r#"{{"model_id":"{model}x"}}"#);
        assert_eq!(compile_local_chat_request(&source), None);
    }
}
