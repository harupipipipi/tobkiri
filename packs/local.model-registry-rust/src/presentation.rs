//! Projection of a model registry response for the Defaults model selector.

use crate::SourceError;
use ordered_python_compat::{PyValue, pystrip, pytruthy};

/// Project the original `present_model_profiles` response used by Settings.
///
/// Registry error envelopes pass through. A malformed successful registry
/// response raises the original validation error, which the HTTP presentation
/// boundary treats as an upstream failure.
pub fn project_list(snapshot: &PyValue) -> Result<PyValue, SourceError> {
    if snapshot.as_object().is_none() {
        let type_name = match snapshot {
            PyValue::Null => "NoneType",
            PyValue::Bool(_) => "bool",
            PyValue::Int(_) => "int",
            PyValue::Float(_) => "float",
            PyValue::String(_) => "str",
            PyValue::Array(_) => "list",
            PyValue::Object(_) => unreachable!("objects implement get"),
        };
        return Err(SourceError::new(
            "AttributeError",
            format!("'{type_name}' object has no attribute 'get'"),
        ));
    }
    if snapshot.get("state").and_then(PyValue::as_str) == Some("error") {
        return Ok(snapshot.clone());
    }
    let profiles = snapshot
        .get("profiles")
        .and_then(PyValue::as_array)
        .ok_or_else(|| invalid("profile list"))?;
    let mut projected = Vec::new();
    for profile in profiles {
        if let Some(record) = project_profile(profile)? {
            projected.push(record);
        }
    }
    let count = PyValue::Int(projected.len().to_string());
    let mut result = vec![
        ("profiles".into(), PyValue::Array(projected)),
        ("count".into(), count),
    ];
    if let Some(revision @ PyValue::Int(_)) = snapshot.get("revision") {
        result.push(("registry_revision".into(), revision.clone()));
    }
    Ok(PyValue::Object(result))
}

fn project_profile(profile: &PyValue) -> Result<Option<PyValue>, SourceError> {
    if profile.as_object().is_none() {
        return Err(invalid("profile"));
    }
    let mut record = Vec::new();
    for (source, destination) in [
        ("model_profile_id", "profile_id"),
        ("display_name", "display_name"),
        ("model_id", "model_id"),
    ] {
        let identity = profile
            .get(source)
            .and_then(PyValue::as_str)
            .filter(|value| !pystrip(value).is_empty())
            .ok_or_else(|| invalid("identity"))?;
        record.push((destination.into(), PyValue::String(identity.into())));
    }
    if let Some(cap) = profile
        .get("parameters")
        .and_then(|parameters| parameters.get("max_tokens"))
        .filter(|cap| !matches!(cap, PyValue::Null))
    {
        let PyValue::Int(digits) = cap else {
            return Err(invalid("output limit"));
        };
        let valid = digits
            .parse::<u32>()
            .is_ok_and(|value| (1..=131072).contains(&value));
        if !valid {
            return Err(invalid("output limit"));
        }
        record.push(("max_output_tokens".into(), cap.clone()));
    }
    let provider = profile
        .get("metadata")
        .and_then(|metadata| metadata.get("provider_connection_id"))
        .filter(|provider| pytruthy(provider))
        .or_else(|| {
            profile
                .get("requirements")
                .and_then(|requirements| requirements.get("preferred_provider_instance_id"))
        })
        .and_then(PyValue::as_str)
        .filter(|provider| !provider.is_empty());
    if let Some(provider) = provider {
        record.push(("provider_id".into(), PyValue::String(provider.into())));
        record.push(("route_configured".into(), PyValue::Bool(true)));
    }
    match profile.get("enabled") {
        Some(PyValue::Bool(true)) => Ok(Some(PyValue::Object(record))),
        Some(PyValue::Bool(false)) => Ok(None),
        _ => Err(invalid("enabled state")),
    }
}

fn invalid(subject: &str) -> SourceError {
    SourceError::new(
        "ValueError",
        format!("model registry returned an invalid {subject}"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::loads;

    fn value(json: &str) -> PyValue {
        loads(json).unwrap()
    }

    #[test]
    fn selects_public_fields_and_preserves_order_and_exact_revision() {
        let snapshot = value(
            r#"{
                "profiles": [
                    {
                        "model_profile_id": "second", "display_name": " Second ",
                        "model_id": "external", "enabled": true,
                        "metadata": {"provider_connection_id": " ", "note": "private"},
                        "requirements": {"preferred_provider_instance_id": "ignored"},
                        "parameters": {"max_tokens": 131072, "private": true},
                        "credential_handle": "opaque"
                    },
                    {
                        "model_profile_id": "disabled", "display_name": "Disabled",
                        "model_id": "external", "enabled": false
                    },
                    {
                        "model_profile_id": "first", "display_name": "First",
                        "model_id": "external", "enabled": true,
                        "parameters": {"max_tokens": 1},
                        "metadata": {"provider_connection_id": ""},
                        "requirements": {"preferred_provider_instance_id": "legacy"}
                    }
                ],
                "count": 99, "revision": 90071992547409931234567890,
                "aliases": {"alias": "second"}
            }"#,
        );
        assert_eq!(
            project_list(&snapshot).unwrap(),
            value(
                r#"{
                    "profiles": [
                        {"profile_id": "second", "display_name": " Second ",
                         "model_id": "external", "max_output_tokens": 131072,
                         "provider_id": " ", "route_configured": true},
                        {"profile_id": "first", "display_name": "First",
                         "model_id": "external", "max_output_tokens": 1,
                         "provider_id": "legacy", "route_configured": true}
                    ],
                    "count": 2, "registry_revision": 90071992547409931234567890
                }"#,
            )
        );
    }

    #[test]
    fn requires_truthy_string_provider_after_legacy_fallback() {
        let profile = |provider: &str, cap: &str, revision: &str| {
            value(&format!(
                r#"{{"profiles": [{{
                    "model_profile_id": "id", "display_name": "name",
                    "model_id": "model", "enabled": true, "parameters": {cap},
                    "metadata": {{"provider_connection_id": {provider}}},
                    "requirements": {{"preferred_provider_instance_id": "legacy"}}
                }}], "revision": {revision}}}"#,
            ))
        };
        for provider in ["1", "true", r#"{"x": false}"#, "[null]"] {
            let snapshot = profile(provider, r#"{"max_tokens": null}"#, "true");
            assert_eq!(
                project_list(&snapshot).unwrap(),
                value(
                    r#"{"profiles":[{"profile_id":"id","display_name":"name","model_id":"model"}],
                        "count":1}"#,
                )
            );
        }
        for provider in ["0", "false", "null", "[]", "{}", r#""""#] {
            let snapshot = profile(provider, "[]", "0.0");
            assert_eq!(
                project_list(&snapshot).unwrap(),
                value(
                    r#"{"profiles":[{"profile_id":"id","display_name":"name","model_id":"model",
                        "provider_id":"legacy","route_configured":true}],"count":1}"#,
                )
            );
        }
    }

    #[test]
    fn validates_successes_before_skipping_disabled_profiles() {
        for (json, subject) in [
            (r#"{}"#, "profile list"),
            (r#"{"profiles": null}"#, "profile list"),
            (r#"{"profiles": [null]}"#, "profile"),
            (r#"{"profiles": [{}]}"#, "identity"),
            (
                r#"{"profiles": [{"model_profile_id":"\u001c\u00a0","display_name":"name",
                    "model_id":"m","enabled":false}]}"#,
                "identity",
            ),
            (
                r#"{"profiles": [{"model_profile_id":"p","display_name":"name","model_id":"m",
                    "enabled":1}]}"#,
                "enabled state",
            ),
        ] {
            let error = project_list(&value(json)).unwrap_err();
            assert_eq!(error.exception_class, "ValueError");
            assert_eq!(
                error.message,
                format!("model registry returned an invalid {subject}")
            );
        }
        for cap in ["true", "0", "-1", "131073", "1.0", r#""1""#, "90200000000"] {
            let snapshot = value(&format!(
                r#"{{"profiles": [{{"model_profile_id":"p","display_name":"name","model_id":"m",
                    "enabled":false,"parameters":{{"max_tokens": {cap}}}}}]}}"#,
            ));
            let error = project_list(&snapshot).unwrap_err();
            assert_eq!(error.exception_class, "ValueError");
            assert_eq!(
                error.message,
                "model registry returned an invalid output limit"
            );
        }
    }

    #[test]
    fn preserves_error_envelopes_and_the_shape_of_empty_successes() {
        let error = value(r#"{"state":"error","code":"authority_denied","profiles":null}"#);
        assert_eq!(project_list(&error).unwrap(), error);
        assert_eq!(
            project_list(&value(r#"{"profiles":[],"revision":0}"#)).unwrap(),
            value(r#"{"profiles":[],"count":0,"registry_revision":0}"#)
        );
        let invalid = project_list(&PyValue::Null).unwrap_err();
        assert_eq!(invalid.exception_class, "AttributeError");
        assert_eq!(invalid.message, "'NoneType' object has no attribute 'get'");
    }
}
