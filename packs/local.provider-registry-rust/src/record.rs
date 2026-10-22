//! Pure helpers translated from the canonical Registry and local endpoint owner.

use crate::failure::{SourceError, SourceResult};
use ordered_python_compat::{PyValue, pystr, pystrip, pytruthy};
use provider_model_access::registry_support::{self as compiler, get, mapping};

/// Apply the original profile path validation, including Python str/or/strip.
pub fn validate_profile_id(value: &PyValue) -> SourceResult<String> {
    let candidate = string_or(value, "");
    let candidate = pystrip(&candidate);
    if candidate.is_empty() {
        return Err(SourceError::value("profile_id must not be empty"));
    }
    if candidate.contains(['/', '\\']) {
        return Err(SourceError::value(
            "profile_id must not contain path separators",
        ));
    }
    if candidate.contains("..") {
        return Err(SourceError::value(
            "profile_id must not contain path traversal segments",
        ));
    }
    Ok(candidate.into())
}

/// Normalize the Registry's ASCII identifier after Python truthiness/coercion.
pub fn identifier(value: &PyValue) -> SourceResult<String> {
    let result = string_or(value, "");
    let result = pystrip(&result);
    if !compiler::identifier(result, 256, false) {
        return Err(SourceError::value(
            "provider registry identifier is invalid",
        ));
    }
    Ok(result.into())
}

pub fn normalize_model_access(value: &PyValue, connection: &PyValue) -> SourceResult<PyValue> {
    compiler::normalize_model_access(value, connection).map_err(Into::into)
}

pub fn effective_model_access(value: &PyValue) -> SourceResult<PyValue> {
    compiler::effective_model_access(value).map_err(Into::into)
}

/// Preserve the original record field order and validation precedence.
pub fn normalize_record(value: &PyValue) -> SourceResult<PyValue> {
    let enabled = get(value, "enabled")?;
    let default_enabled = PyValue::Bool(true);
    let enabled = if value.get("enabled").is_some() {
        enabled
    } else {
        &default_enabled
    };
    if !matches!(enabled, PyValue::Bool(_)) {
        return Err(SourceError::value("provider enabled must be a boolean"));
    }
    let provider_instance_id = identifier(get(value, "provider_instance_id")?)?;
    let adapter_id = identifier(get(value, "adapter_id")?)?;
    let credential_handle = get(value, "credential_handle")?;
    if !matches!(credential_handle, PyValue::Null)
        && !["credential:", "opaque:"]
            .iter()
            .any(|prefix| pystr(credential_handle).starts_with(*prefix))
    {
        return Err(SourceError::value(
            "provider credential must be an opaque handle",
        ));
    }
    let endpoint = provider_endpoint(
        get(value, "endpoint")?,
        credential_handle,
        &PyValue::String(adapter_id.clone()),
    )?;
    let health = get(value, "health_evidence")?;
    let empty = PyValue::Object(Vec::new());
    let health = if health.as_object().is_some() {
        health
    } else {
        &empty
    };
    let default_verified = PyValue::Bool(false);
    let verified = health.get("verified").unwrap_or(&default_verified);
    if !matches!(verified, PyValue::Bool(_)) {
        return Err(SourceError::value(
            "provider health verified must be a boolean",
        ));
    }
    let mut normalized = PyValue::object([
        (
            "provider_instance_id".into(),
            PyValue::String(provider_instance_id.clone()),
        ),
        ("adapter_id".into(), PyValue::String(adapter_id)),
        (
            "display_name".into(),
            PyValue::String(truncated_string(
                get(value, "display_name")?,
                &provider_instance_id,
                200,
            )),
        ),
        ("credential_handle".into(), credential_handle.clone()),
        ("endpoint".into(), endpoint),
        ("enabled".into(), enabled.clone()),
        (
            "data_residency".into(),
            PyValue::String(truncated_string(
                get(value, "data_residency")?,
                "unknown",
                100,
            )),
        ),
        (
            "health_evidence".into(),
            PyValue::object([
                (
                    "status".into(),
                    PyValue::String(string_or(get(health, "status")?, "unknown")),
                ),
                ("observed_at".into(), get(health, "observed_at")?.clone()),
                ("verified".into(), verified.clone()),
            ]),
        ),
        ("metadata".into(), safe_metadata(get(value, "metadata")?)),
    ]);
    let policy = effective_model_access(value)?;
    if !matches!(policy, PyValue::Null) {
        let policy = normalize_model_access(&policy, &normalized)?;
        set_field(&mut normalized, "model_access", policy)?;
    }
    Ok(normalized)
}

/// Carry policy only inside the exact adapter/catalog/endpoint route namespace.
pub fn retain_model_access(normalized: &mut PyValue, current: &PyValue) -> SourceResult<()> {
    if compiler::contains(normalized, "model_access")? {
        return Ok(());
    }
    let existing = effective_model_access(current)?;
    if matches!(existing, PyValue::Null) {
        return Ok(());
    }
    let previous = model_namespace(current)?;
    let proposed = model_namespace(normalized)?;
    if ![
        (&previous.0, &proposed.0),
        (&previous.1, &proposed.1),
        (&previous.2, &proposed.2),
    ]
    .into_iter()
    .all(|(left, right)| namespace_equal(left, right))
    {
        return Err(SourceError::value(
            "provider route changed; create a new connection or explicitly replace model access for the new connection",
        ));
    }
    let policy = normalize_model_access(&existing, normalized)?;
    set_field(normalized, "model_access", policy)
}

fn model_namespace(value: &PyValue) -> SourceResult<(PyValue, PyValue, PyValue)> {
    let metadata = get(value, "metadata")?;
    let catalog_id = metadata
        .get("catalog_provider_id")
        .cloned()
        .unwrap_or(PyValue::Null);
    Ok((
        get(value, "adapter_id")?.clone(),
        catalog_id,
        get(value, "endpoint")?.clone(),
    ))
}

/// Validate a route without replacing the original remote endpoint spelling.
pub fn provider_endpoint(
    endpoint: &PyValue,
    credential: &PyValue,
    adapter: &PyValue,
) -> SourceResult<PyValue> {
    if adapter.as_str() == Some("local-openai-compatible") {
        if !matches!(credential, PyValue::Null) {
            return Err(SourceError::value(
                "local provider credentials are not permitted",
            ));
        }
        return local_endpoint(endpoint).map(PyValue::String);
    }
    if matches!(endpoint, PyValue::Null) {
        return Ok(PyValue::Null);
    }
    let text = pystr(endpoint);
    let parsed = compiler::split_url(&text)?;
    if !["http", "https"].contains(&parsed.scheme.as_str())
        || parsed.hostname.is_none()
        || parsed.userinfo
    {
        return Err(SourceError::value(
            "provider endpoint must be a canonical HTTP(S) URL",
        ));
    }
    if !matches!(credential, PyValue::Null) && parsed.scheme != "https" {
        return Err(SourceError::value(
            "credentialed provider endpoint requires HTTPS",
        ));
    }
    Ok(PyValue::String(text))
}

fn local_endpoint(value: &PyValue) -> SourceResult<String> {
    let invalid = || SourceError::value("local provider endpoint is invalid");
    let text = value
        .as_str()
        .filter(|text| !text.is_empty() && text.chars().count() <= 2048)
        .ok_or_else(invalid)?;
    let parsed = compiler::split_url(text).map_err(|_| invalid())?;
    let port = parsed.port().map_err(|_| invalid())?;
    let host = parsed.hostname.as_deref();
    if parsed.scheme != "http"
        || !matches!(host, Some("127.0.0.1" | "::1"))
        || parsed.userinfo
        || port.is_none_or(|port| port < 1024)
        || !["/v1", "/v1/"].contains(&parsed.path.as_str())
        || !parsed.query.is_empty()
        || !parsed.fragment.is_empty()
        || text.chars().any(|ch| pystrip(&ch.to_string()).is_empty())
    {
        return Err(invalid());
    }
    let host = if host == Some("::1") {
        "[::1]"
    } else {
        "127.0.0.1"
    };
    let canonical = format!(
        "http://{host}:{}/v1",
        port.expect("validated local endpoint port")
    );
    if text != canonical && text != format!("{canonical}/") {
        return Err(invalid());
    }
    Ok(canonical)
}

fn safe_metadata(value: &PyValue) -> PyValue {
    let Some(entries) = value.as_object() else {
        return PyValue::Object(Vec::new());
    };
    PyValue::object(
        entries
            .iter()
            .filter(|(key, value)| {
                !["secret", "token", "password", "api_key", "authorization"]
                    .iter()
                    .any(|marker| key.to_lowercase().contains(*marker))
                    && !matches!(value, PyValue::Array(_) | PyValue::Object(_))
            })
            .cloned(),
    )
}

fn set_field(value: &mut PyValue, key: &str, replacement: PyValue) -> SourceResult<()> {
    mapping(value)?;
    let PyValue::Object(entries) = value else {
        unreachable!("mapping checked above")
    };
    if let Some((_, current)) = entries.iter_mut().find(|(name, _)| name == key) {
        *current = replacement;
    } else {
        entries.push((key.into(), replacement));
    }
    Ok(())
}

fn string_or(value: &PyValue, fallback: &str) -> String {
    if pytruthy(value) {
        pystr(value)
    } else {
        fallback.into()
    }
}

fn truncated_string(value: &PyValue, fallback: &str, count: usize) -> String {
    string_or(value, fallback).chars().take(count).collect()
}

fn namespace_equal(left: &PyValue, right: &PyValue) -> bool {
    use ordered_python_compat::pyint;
    match (left, right) {
        (PyValue::Bool(left), PyValue::Int(right)) | (PyValue::Int(right), PyValue::Bool(left)) => {
            right.as_str() == if *left { "1" } else { "0" }
        }
        (PyValue::Bool(left), PyValue::Float(right))
        | (PyValue::Float(right), PyValue::Bool(left)) => *right == if *left { 1.0 } else { 0.0 },
        (PyValue::Int(left), PyValue::Float(right)) => {
            right.is_finite()
                && right.fract() == 0.0
                && pyint(&PyValue::Float(*right)).is_ok_and(|right| left == &right)
        }
        (PyValue::Float(_), PyValue::Int(_)) => namespace_equal(right, left),
        _ => left == right,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> PyValue {
        PyValue::String(value.into())
    }

    fn connection() -> PyValue {
        PyValue::object([
            ("provider_instance_id".into(), text("connection/work")),
            ("adapter_id".into(), text("openai-compatible")),
            ("credential_handle".into(), text("credential:fixture")),
            ("endpoint".into(), text("https://provider.example/v1")),
        ])
    }

    fn all_policy() -> PyValue {
        PyValue::object([
            ("version".into(), text("tobkiri.connection-model-access.v1")),
            ("mode".into(), text("all")),
            ("model_ids".into(), PyValue::Array(Vec::new())),
        ])
    }

    #[test]
    fn record_errors_follow_enabled_then_identifier_then_credential_order() {
        let error = normalize_record(&PyValue::Null).unwrap_err();
        assert_eq!(error.exception_class, "AttributeError");
        assert_eq!(error.message, "'NoneType' object has no attribute 'get'");
        let mut input = connection();
        set_field(&mut input, "provider_instance_id", text("")).unwrap();
        set_field(&mut input, "credential_handle", text("invalid")).unwrap();
        set_field(&mut input, "enabled", PyValue::Int("1".into())).unwrap();
        assert_eq!(
            normalize_record(&input).unwrap_err().message,
            "provider enabled must be a boolean"
        );
        set_field(&mut input, "enabled", PyValue::Bool(true)).unwrap();
        assert_eq!(
            normalize_record(&input).unwrap_err().message,
            "provider registry identifier is invalid"
        );
    }

    #[test]
    fn profile_and_identifier_use_python_truthiness_and_unicode_strip() {
        assert_eq!(
            validate_profile_id(&text("\u{1c}defaults\u{3000}")).unwrap(),
            "defaults"
        );
        assert_eq!(
            validate_profile_id(&text("../foreign"))
                .unwrap_err()
                .message,
            "profile_id must not contain path separators"
        );
        assert!(validate_profile_id(&PyValue::Bool(false)).is_err());
        assert!(identifier(&PyValue::Int("0".into())).is_err());
        assert_eq!(identifier(&PyValue::Int("1".into())).unwrap(), "1");
    }

    #[test]
    fn remote_endpoint_keeps_spelling_and_null_even_when_a_handle_exists() {
        let credential = text("credential:fixture");
        let adapter = text("openai-compatible");
        let endpoint = text("HTTPS://Provider.Example/v1/");
        assert_eq!(
            provider_endpoint(&endpoint, &credential, &adapter).unwrap(),
            endpoint
        );
        assert_eq!(
            provider_endpoint(&PyValue::Null, &credential, &adapter).unwrap(),
            PyValue::Null
        );
        assert_eq!(
            provider_endpoint(&text("http://provider.example/v1"), &credential, &adapter)
                .unwrap_err()
                .message,
            "credentialed provider endpoint requires HTTPS"
        );
    }

    #[test]
    fn local_endpoint_checks_credentials_first_and_requires_canonical_spelling() {
        let adapter = text("local-openai-compatible");
        assert_eq!(
            provider_endpoint(&PyValue::Null, &text("opaque:fixture"), &adapter)
                .unwrap_err()
                .message,
            "local provider credentials are not permitted"
        );
        assert_eq!(
            provider_endpoint(&text("http://[::1]:1234/v1/"), &PyValue::Null, &adapter).unwrap(),
            text("http://[::1]:1234/v1")
        );
        assert!(
            provider_endpoint(&text("HTTP://127.0.0.1:1234/v1"), &PyValue::Null, &adapter).is_err()
        );
        assert!(
            provider_endpoint(&text("http://127.0.0.1:01234/v1"), &PyValue::Null, &adapter)
                .is_err()
        );
    }

    #[test]
    fn record_retains_safe_scalars_and_counts_string_limits_in_characters() {
        let mut input = connection();
        set_field(&mut input, "display_name", text(&"あ".repeat(201))).unwrap();
        set_field(
            &mut input,
            "metadata",
            PyValue::object([
                ("count".into(), PyValue::Int("3".into())),
                ("enabled".into(), PyValue::Bool(false)),
                ("Access_Token_Hint".into(), text("synthetic")),
                ("nested".into(), PyValue::Object(Vec::new())),
            ]),
        )
        .unwrap();
        let normalized = normalize_record(&input).unwrap();
        assert_eq!(
            normalized
                .get("display_name")
                .and_then(PyValue::as_str)
                .unwrap()
                .chars()
                .count(),
            200
        );
        assert_eq!(
            normalized.get("metadata").unwrap(),
            &PyValue::object([
                ("count".into(), PyValue::Int("3".into())),
                ("enabled".into(), PyValue::Bool(false)),
            ])
        );
        assert_eq!(normalized.get("enabled"), Some(&PyValue::Bool(true)));
    }

    #[test]
    fn retained_policy_uses_python_numeric_namespace_equality() {
        let mut input = connection();
        set_field(
            &mut input,
            "metadata",
            PyValue::object([("catalog_provider_id".into(), PyValue::Bool(false))]),
        )
        .unwrap();
        set_field(&mut input, "model_access", all_policy()).unwrap();
        let current = normalize_record(&input).unwrap();
        let mut proposed = connection();
        set_field(
            &mut proposed,
            "metadata",
            PyValue::object([("catalog_provider_id".into(), PyValue::Int("0".into()))]),
        )
        .unwrap();
        let mut proposed = normalize_record(&proposed).unwrap();
        retain_model_access(&mut proposed, &current).unwrap();
        assert_eq!(proposed.get("model_access"), Some(&all_policy()));
        assert!(!namespace_equal(
            &PyValue::Int("9007199254740993".into()),
            &PyValue::Float(9007199254740992.0)
        ));
    }

    #[test]
    fn route_change_requires_explicit_policy_replacement() {
        let mut input = connection();
        set_field(&mut input, "model_access", all_policy()).unwrap();
        let current = normalize_record(&input).unwrap();
        let mut proposed = connection();
        set_field(&mut proposed, "endpoint", text("https://other.example/v1")).unwrap();
        let mut proposed = normalize_record(&proposed).unwrap();
        assert!(retain_model_access(&mut proposed, &current).is_err());
        set_field(&mut proposed, "model_access", all_policy()).unwrap();
        retain_model_access(&mut proposed, &current).unwrap();
    }
}
