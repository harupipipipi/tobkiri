//! The Defaults Settings model registration request and response.

use crate::{SourceError, presentation::project_list};
use ordered_python_compat::{PyValue, pystrip};

const REQUIRED_FIELDS: [&str; 6] = [
    "model_profile_id",
    "model_id",
    "provider_instance_id",
    "display_name",
    "expected_revision",
    "provider_registry_revision",
];
const OUTPUT_LIMIT: &str = "max_output_tokens";

/// Validate Settings input and form the original registry operation.
pub(crate) fn normalize(payload: &PyValue) -> Result<PyValue, SourceError> {
    let Some(fields) = payload.as_object() else {
        return Err(value_error("model configuration fields are invalid"));
    };
    if fields
        .iter()
        .any(|(key, _)| key != OUTPUT_LIMIT && !REQUIRED_FIELDS.contains(&key.as_str()))
    {
        return Err(value_error("model configuration fields are invalid"));
    }
    let [
        Some(profile_id),
        Some(model_id),
        Some(provider_id),
        Some(display_name),
        Some(revision),
        Some(provider_revision),
    ] = REQUIRED_FIELDS.map(|field| payload.get(field))
    else {
        return Err(value_error("model configuration fields are invalid"));
    };

    let cap = payload.get(OUTPUT_LIMIT);
    if let Some(cap) = cap {
        let PyValue::Int(digits) = cap else {
            return Err(value_error("model output token limit is invalid"));
        };
        if !digits
            .parse::<u32>()
            .is_ok_and(|value| (1..=131072).contains(&value))
        {
            return Err(value_error("model output token limit is invalid"));
        }
    }
    if !nonnegative_integer(revision) {
        return Err(value_error("model configuration revision is invalid"));
    }
    if !nonnegative_integer(provider_revision) {
        return Err(value_error("provider registry revision is invalid"));
    }
    if ![profile_id, model_id, provider_id]
        .into_iter()
        .all(|value| value.as_str().is_some_and(valid_identifier))
    {
        return Err(value_error("model configuration identity is invalid"));
    }
    if !display_name
        .as_str()
        .is_some_and(|name| !pystrip(name).is_empty() && name.chars().nth(200).is_none())
    {
        return Err(value_error("model configuration name is invalid"));
    }

    let operation = if cap.is_some() { "save" } else { "create" };
    let mut request = vec![
        ("operation".into(), PyValue::String(operation.into())),
        ("expected_revision".into(), revision.clone()),
        (
            "provider_registry_revision".into(),
            provider_revision.clone(),
        ),
    ];
    let mut record = vec![
        ("model_profile_id".into(), profile_id.clone()),
        ("model_id".into(), model_id.clone()),
        ("display_name".into(), display_name.clone()),
        (
            "metadata".into(),
            PyValue::Object(vec![("provider_connection_id".into(), provider_id.clone())]),
        ),
    ];
    // An explicit output limit updates the route without replacing its policy.
    if let Some(cap) = cap {
        request.push(("preserve_existing".into(), PyValue::Bool(true)));
        record.extend([
            ("enabled".into(), PyValue::Bool(true)),
            (
                "parameters".into(),
                PyValue::Object(vec![("max_tokens".into(), cap.clone())]),
            ),
        ]);
    }
    request.push(("record".into(), PyValue::Object(record)));
    Ok(PyValue::Object(request))
}

/// Reproduce the captured provider response for a normalized Settings request.
pub(crate) fn captured_save_result(
    request: &PyValue,
    result: &PyValue,
) -> Result<PyValue, SourceError> {
    if request.get("operation").and_then(PyValue::as_str) != Some("create") {
        return Ok(result.clone());
    }

    let profile = item(result, "profile")?;
    let action = item(result, "action")?;
    let revision = item(result, "store_revision")?;
    let mut selected = Vec::new();
    for key in ["model_profile_id", "model_id", "display_name", "enabled"] {
        selected.push((key.into(), item(profile, key)?.clone()));
    }
    let record = item(request, "record")?;
    let provider = item(item(record, "metadata")?, "provider_connection_id")?;
    selected.push((
        "metadata".into(),
        PyValue::Object(vec![("provider_connection_id".into(), provider.clone())]),
    ));
    Ok(PyValue::Object(vec![
        ("action".into(), action.clone()),
        ("store_revision".into(), revision.clone()),
        ("profile".into(), PyValue::Object(selected)),
    ]))
}

/// Present the saved registry profile using the same fields as the selector.
pub(crate) fn present_saved(result: &PyValue) -> Result<PyValue, SourceError> {
    if result.as_object().is_none()
        || result.get("state").and_then(PyValue::as_str) == Some("error")
    {
        return project_list(result);
    }

    let revision = result.get("store_revision");
    let Some(revision @ PyValue::Int(_)) = revision else {
        return Err(value_error("model configuration save was not confirmed"));
    };
    if result.get("action").and_then(PyValue::as_str) != Some("saved") {
        return Err(value_error("model configuration save was not confirmed"));
    }
    let profile = result.get("profile").cloned().unwrap_or(PyValue::Null);
    project_list(&PyValue::Object(vec![
        ("profiles".into(), PyValue::Array(vec![profile])),
        ("revision".into(), revision.clone()),
    ]))
}

fn valid_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 256
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || b"._:/-".contains(&byte))
}

fn nonnegative_integer(value: &PyValue) -> bool {
    matches!(value, PyValue::Int(digits) if !digits.starts_with('-'))
}

fn item<'a>(value: &'a PyValue, key: &str) -> Result<&'a PyValue, SourceError> {
    let message = match value {
        PyValue::Object(_) => {
            return value
                .get(key)
                .ok_or_else(|| SourceError::new("KeyError", format!("'{key}'")));
        }
        PyValue::Null => "'NoneType' object is not subscriptable",
        PyValue::Bool(_) => "'bool' object is not subscriptable",
        PyValue::Int(_) => "'int' object is not subscriptable",
        PyValue::Float(_) => "'float' object is not subscriptable",
        PyValue::String(_) => "string indices must be integers, not 'str'",
        PyValue::Array(_) => "list indices must be integers or slices, not str",
    };
    Err(SourceError::new("TypeError", message))
}

fn value_error(message: &str) -> SourceError {
    SourceError::new("ValueError", message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::loads;

    fn value(json: &str) -> PyValue {
        loads(json).unwrap()
    }

    #[test]
    fn normalizes_selector_create_and_output_policy_save() {
        let input = value(
            r#"{"display_name":" Daily 🌱 ","expected_revision":123456789012345678901,
                "provider_registry_revision":0,"provider_instance_id":"p:test/local",
                "model_id":"models/gemma-2","model_profile_id":"daily.model_2"}"#,
        );
        assert_eq!(
            normalize(&input).unwrap(),
            value(
                r#"{"operation":"create","expected_revision":123456789012345678901,
                    "provider_registry_revision":0,
                    "record":{"model_profile_id":"daily.model_2","model_id":"models/gemma-2",
                    "display_name":" Daily 🌱 ","metadata":{"provider_connection_id":"p:test/local"}}}"#,
            ),
        );

        let mut fields = input.into_object().unwrap();
        fields.push((OUTPUT_LIMIT.into(), PyValue::Int("131072".into())));
        assert_eq!(
            normalize(&PyValue::Object(fields)).unwrap(),
            value(
                r#"{"operation":"save","expected_revision":123456789012345678901,
                    "provider_registry_revision":0,"preserve_existing":true,
                    "record":{"model_profile_id":"daily.model_2","model_id":"models/gemma-2",
                    "display_name":" Daily 🌱 ","metadata":{"provider_connection_id":"p:test/local"},
                    "enabled":true,"parameters":{"max_tokens":131072}}}"#,
            ),
        );
    }

    #[test]
    fn validates_fields_cap_revisions_identity_and_name_in_order() {
        let input = value(
            r#"{"model_profile_id":"daily\n","model_id":"model","provider_instance_id":"p",
                "display_name":"\u001c\u00a0","expected_revision":true,
                "provider_registry_revision":-1,"max_output_tokens":null,
                "credential_handle":"opaque"}"#,
        );
        let mut fields = input.into_object().unwrap();
        for (field, replacement, message) in [
            (
                "credential_handle",
                None,
                "model configuration fields are invalid",
            ),
            (
                OUTPUT_LIMIT,
                Some("1"),
                "model output token limit is invalid",
            ),
            (
                "expected_revision",
                Some("0"),
                "model configuration revision is invalid",
            ),
            (
                "provider_registry_revision",
                Some("0"),
                "provider registry revision is invalid",
            ),
            (
                "model_profile_id",
                Some(r#""daily""#),
                "model configuration identity is invalid",
            ),
            (
                "display_name",
                Some(r#""Daily""#),
                "model configuration name is invalid",
            ),
        ] {
            assert_eq!(
                normalize(&PyValue::Object(fields.clone())).unwrap_err(),
                value_error(message),
            );
            fields.retain(|(key, _)| key != field);
            if let Some(replacement) = replacement {
                fields.push((field.into(), value(replacement)));
            }
        }
        normalize(&PyValue::Object(fields)).unwrap();
    }

    #[test]
    fn requires_exact_integer_caps_and_counts_name_in_unicode_characters() {
        let payload = |identity: &str, name: &str, cap: &str| {
            value(&format!(
                r#"{{"model_profile_id":"{identity}","model_id":"model","provider_instance_id":"p",
                    "display_name":"{name}","expected_revision":0,"provider_registry_revision":0,
                    "max_output_tokens":{cap}}}"#,
            ))
        };
        for cap in [
            "true", "false", "0", "-1", "131073", "1.0", r#""1""#, "null",
        ] {
            assert_eq!(
                normalize(&payload("p", "Daily", cap)).unwrap_err(),
                value_error("model output token limit is invalid"),
            );
        }
        normalize(&payload(&"p".repeat(256), &"🌱".repeat(200), "1")).unwrap();
        assert_eq!(
            normalize(&payload(&"p".repeat(257), "Daily", "1")).unwrap_err(),
            value_error("model configuration identity is invalid"),
        );
        assert_eq!(
            normalize(&payload("p", &"🌱".repeat(201), "1")).unwrap_err(),
            value_error("model configuration name is invalid"),
        );
    }

    #[test]
    fn projects_only_confirmed_saves_and_keeps_error_envelopes() {
        let error = value(r#"{"state":"error","code":"authority_denied","action":"saved"}"#);
        assert_eq!(present_saved(&error).unwrap(), error);
        let saved = value(
            r#"{"action":"saved","store_revision":-1,"profile":{
                "model_profile_id":"p","display_name":"Daily","model_id":"m","enabled":true,
                "parameters":{"max_tokens":2048,"temperature":0.2},
                "metadata":{"provider_connection_id":"test","private":true},
                "credential_handle":"opaque"}}"#,
        );
        assert_eq!(
            present_saved(&saved).unwrap(),
            value(
                r#"{"profiles":[{"profile_id":"p","display_name":"Daily","model_id":"m",
                    "max_output_tokens":2048,"provider_id":"test","route_configured":true}],
                    "count":1,"registry_revision":-1}"#,
            ),
        );
        for invalid in [
            r#"{"action":"created","store_revision":1}"#,
            r#"{"action":"saved","store_revision":true}"#,
            r#"{"action":"saved","store_revision":1.0}"#,
        ] {
            assert_eq!(
                present_saved(&value(invalid)).unwrap_err(),
                value_error("model configuration save was not confirmed"),
            );
        }
        assert_eq!(
            present_saved(&value(r#"{"action":"saved","store_revision":1}"#)).unwrap_err(),
            value_error("model registry returned an invalid profile"),
        );
    }

    #[test]
    fn narrows_existing_create_confirmations_before_settings_projection() {
        let request =
            value(r#"{"operation":"create","record":{"metadata":{"provider_connection_id":"p"}}}"#);
        let result = value(
            r#"{"action":"saved","store_revision":2,"profile":{
                "model_profile_id":"daily","model_id":"gemma","display_name":"Daily",
                "enabled":true,"parameters":{"max_tokens":2048,"temperature":0.2},
                "metadata":{"note":"private"},"credential_handle":"opaque"}}"#,
        );
        let captured = captured_save_result(&request, &result).unwrap();
        assert_eq!(
            captured,
            value(
                r#"{"action":"saved","store_revision":2,"profile":{
                    "model_profile_id":"daily","model_id":"gemma","display_name":"Daily",
                    "enabled":true,"metadata":{"provider_connection_id":"p"}}}"#,
            ),
        );
        assert_eq!(
            present_saved(&captured).unwrap(),
            value(
                r#"{"profiles":[{"profile_id":"daily","display_name":"Daily","model_id":"gemma",
                    "provider_id":"p","route_configured":true}],"count":1,"registry_revision":2}"#,
            ),
        );
        assert_eq!(
            captured_save_result(&value(r#"{"operation":"save"}"#), &result).unwrap(),
            result,
        );

        let missing_field = value(
            r#"{"action":"saved","store_revision":2,"profile":{
                "model_profile_id":"daily","model_id":"gemma","display_name":"Daily"}}"#,
        );
        assert_eq!(
            captured_save_result(&request, &missing_field).unwrap_err(),
            SourceError::new("KeyError", "'enabled'"),
        );
    }
}
