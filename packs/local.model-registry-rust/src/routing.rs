//! Project one saved model for the keyless local OpenAI-compatible text route.

use ordered_python_compat::PyValue;

use crate::{
    SourceError,
    registry::{Fields, Registry, field, identifier, object, string},
};

pub(crate) fn resolve_model(
    registry: &Registry,
    identifier_value: String,
) -> Result<PyValue, SourceError> {
    let resolved = registry.resolve(&PyValue::String(identifier_value))?;
    let profile = resolved.get("profile").ok_or_else(|| {
        SourceError::new("ModelRouteNotFound", "saved model profile is unavailable")
    })?;
    if profile.get("enabled") != Some(&PyValue::Bool(true)) {
        return Err(SourceError::new(
            "ModelRouteDenied",
            "saved model profile is disabled",
        ));
    }

    exact_identifier(resolved.get("resolved_profile_id"))
        .ok_or_else(|| invalid("saved model profile identity is invalid"))?;
    let model = exact_identifier(profile.get("model_id"))
        .ok_or_else(|| invalid("saved model identity is invalid"))?;
    let requirements = fields(
        profile.get("requirements"),
        "saved model requirements are invalid",
    )?;
    let metadata = fields(profile.get("metadata"), "saved model metadata is invalid")?;
    let connection = match field(metadata, "provider_connection_id") {
        None | Some(PyValue::Null) => field(requirements, "preferred_provider_instance_id"),
        explicit => explicit,
    };
    // Settings recognizes the older preference as a connection reference.
    // The provider owner must resolve and authorize this exact ID on each call.
    let connection = exact_identifier(connection)
        .ok_or_else(|| invalid("saved model provider connection is invalid"))?;
    validate_requirements(requirements, model)?;

    let parameters = fields(
        profile.get("parameters"),
        "saved model parameters are invalid",
    )?;
    let (cap, temperature) = project_parameters(parameters)?;
    Ok(object([
        ("model_id", string(model)),
        ("provider_instance_id", string(connection)),
        ("max_tokens", cap),
        ("temperature", temperature),
    ]))
}

fn fields<'a>(
    value: Option<&'a PyValue>,
    message: &'static str,
) -> Result<&'a Fields, SourceError> {
    match value {
        None | Some(PyValue::Null) => Ok(&[]),
        Some(value) => value.as_object().ok_or_else(|| invalid(message)),
    }
}

fn exact_identifier(value: Option<&PyValue>) -> Option<&str> {
    let value = value?;
    let raw = value.as_str()?;
    (identifier(value).ok()?.as_ref() == raw).then_some(raw)
}

fn validate_requirements(requirements: &Fields, model: &str) -> Result<(), SourceError> {
    // A single plain-text chat completion satisfies only these requirements.
    // Saved cost, context, residency, and other selection policies need a
    // route that can evaluate them before sending the request.
    for (key, value) in requirements {
        let supported = match key.as_str() {
            "preferred_provider_instance_id" => true,
            "preferred_model_id" => value.as_str() == Some(model),
            "modalities" => value
                .as_array()
                .is_some_and(|values| values.iter().all(|value| value.as_str() == Some("text"))),
            "capabilities" => value.as_array().is_some_and(<[PyValue]>::is_empty),
            "tool_calling" | "thinking" => matches!(value, PyValue::Bool(false)),
            "minimum_context" => matches!(value, PyValue::Int(digits) if digits == "0"),
            "request_surface" => value.as_str() == Some("chat"),
            _ => false,
        };
        if !supported {
            return Err(invalid("saved model requirements are unsupported"));
        }
    }
    Ok(())
}

fn project_parameters(parameters: &Fields) -> Result<(PyValue, PyValue), SourceError> {
    let mut cap = PyValue::Null;
    let mut temperature = PyValue::Null;
    for (key, value) in parameters {
        match (key.as_str(), value) {
            ("max_tokens", PyValue::Int(digits))
                if digits
                    .parse::<u32>()
                    .is_ok_and(|value| (1..=131072).contains(&value)) =>
            {
                cap = value.clone();
            }
            ("max_tokens", _) => return Err(invalid("saved model output limit is invalid")),
            // The local request produces one completion without sampling candidates.
            ("n" | "best_of", PyValue::Int(digits)) if digits == "1" => {}
            ("n" | "best_of", _) => {
                return Err(invalid("multiple model completions are unsupported"));
            }
            ("temperature", _) => {
                let number = match value {
                    PyValue::Int(digits) => digits.parse::<f64>().ok(),
                    PyValue::Float(number) => Some(*number),
                    _ => None,
                }
                .filter(|number| (0.0..=2.0).contains(number))
                .ok_or_else(|| invalid("saved model temperature is invalid"))?;
                // Core StrictV1 JSON carries integers; a decimal string preserves
                // the saved value until the adapter builds the upstream JSON.
                temperature = PyValue::String(number.to_string());
            }
            _ => return Err(invalid("saved model parameters are unsupported")),
        }
    }
    Ok((cap, temperature))
}

fn invalid(message: &'static str) -> SourceError {
    SourceError::new("ModelRouteInvalid", message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::loads;

    fn registry(owner: &str, enabled: bool, fields: &str) -> Registry {
        let document = format!(
            r#"{{
                "version":"rumi.model-registry.store.v1","profile_id":"owner","revision":1,
                "aliases":{{"alias":"saved","chained":"alias","stale":"missing"}},
                "profiles":{{"saved":{{"model_id":"local/model","enabled":{enabled},{fields}}}}}
            }}"#
        );
        Registry::new(owner, Some(loads(&document).unwrap())).unwrap()
    }

    fn resolve(registry: &Registry, key: &str) -> Result<PyValue, SourceError> {
        resolve_model(registry, key.into())
    }

    #[test]
    fn alias_projects_only_the_bound_connection_and_parameters() {
        let registry = registry(
            "owner",
            true,
            r#"
                "requirements":{
                    "preferred_provider_instance_id":"old-connection",
                    "modalities":["text"],"request_surface":"chat","minimum_context":0,
                    "capabilities":[],"tool_calling":false,"thinking":false
                },
                "parameters":{"max_tokens":131072,"temperature":0.25,"n":1,"best_of":1},
                "metadata":{"provider_connection_id":"connection/local","private":"opaque:meta"},
                "credential_handle":"opaque:old-credential"
            "#,
        );
        let expected = loads(
            r#"{"model_id":"local/model","provider_instance_id":"connection/local",
                "max_tokens":131072,"temperature":"0.25"}"#,
        )
        .unwrap();
        assert_eq!(resolve(&registry, "alias").unwrap(), expected);
        assert_eq!(resolve(&registry, "saved").unwrap(), expected);
        for identifier in ["Alias", "SAVED", "chained", "stale", "local/model"] {
            assert_eq!(
                resolve(&registry, identifier).unwrap_err().exception_class,
                "ModelRouteNotFound"
            );
        }
    }

    #[test]
    fn owner_and_enabled_state_apply_to_aliases() {
        let fields = r#""metadata":{"provider_connection_id":"connection/local"}"#;
        assert_eq!(
            resolve(&registry("owner", false, fields), "alias")
                .unwrap_err()
                .exception_class,
            "ModelRouteDenied"
        );
        assert_eq!(
            resolve(&registry("foreign", true, fields), "alias")
                .unwrap_err()
                .message,
            "model registry profile binding is invalid"
        );
    }

    #[test]
    fn missing_explicit_connection_uses_legacy_reference_without_defaults() {
        let fields = r#""requirements":{"preferred_provider_instance_id":"connection/legacy"}"#;
        for fields in [
            fields.into(),
            format!(r#"{fields},"metadata":{{"provider_connection_id":null}}"#),
        ] {
            assert_eq!(
                resolve(&registry("owner", true, &fields), "saved").unwrap(),
                loads(
                    r#"{"model_id":"local/model","provider_instance_id":"connection/legacy",
                        "max_tokens":null,"temperature":null}"#
                )
                .unwrap()
            );
        }
        for malformed in [r#""""#, "42", r#"" connection/local ""#] {
            let with_explicit =
                format!(r#"{fields},"metadata":{{"provider_connection_id":{malformed}}}"#);
            assert_eq!(
                resolve(&registry("owner", true, &with_explicit), "saved")
                    .unwrap_err()
                    .message,
                "saved model provider connection is invalid"
            );
        }
    }

    #[test]
    fn unsupported_policies_and_parameters_fail_before_provider_lookup() {
        for unknown in [
            r#""requirements":{"maximum_cost":0}"#,
            r#""requirements":{"tool_calling":true}"#,
            r#""requirements":{"preferred_model_id":"other"}"#,
            r#""requirements":{"modalities":["text","image"]}"#,
            r#""parameters":{"max_tokens":131073}"#,
            r#""parameters":{"max_tokens":32.0}"#,
            r#""parameters":{"max_tokens":null}"#,
            r#""parameters":{"temperature":2.01}"#,
            r#""parameters":{"temperature":-0.01}"#,
            r#""parameters":{"temperature":"1.2"}"#,
            r#""parameters":{"n":true}"#,
            r#""parameters":{"best_of":2}"#,
            r#""parameters":{"top_p":0.5}"#,
            r#""parameters":{"max_output_tokens":32}"#,
        ] {
            let fields =
                format!(r#""metadata":{{"provider_connection_id":"connection/local"}},{unknown}"#);
            assert_eq!(
                resolve(&registry("owner", true, &fields), "saved")
                    .unwrap_err()
                    .exception_class,
                "ModelRouteInvalid",
                "{unknown}"
            );
        }
    }
}
