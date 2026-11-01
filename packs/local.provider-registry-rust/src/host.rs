//! Pure translation of the Registry Host's request boundary and UI projection.
//!
//! These helpers operate on an already-selected in-memory Registry. They do
//! not create Host contributions, resolve bindings, open stores, or confer
//! Gate authority. The capture check below reports only source validation and
//! contribution cardinality; its fixture facts are not production bindings.

use crate::{
    failure::{SourceError, SourceResult},
    record::validate_profile_id,
    registry::Registry,
    state::{Clock, DocumentStore, NONE, get},
};
use ordered_python_compat::{PyValue, pystrip, pytruthy};

pub const READONLY_FUNCTION_ID: &str = "rumi_provider_registry_pack.provider-registry.resource";
pub const READONLY_CONTRACT_ID: &str = "tobkiri.resource.ai.provider.registry.v1";
pub const READONLY_RESOURCE_OPERATION: &str =
    "rumi_provider_registry_pack.provider-registry-resource";
pub const READONLY_GENERATE_OPERATION: &str =
    "rumi_provider_registry_pack.provider-registry-resource.generate";
pub const READONLY_STREAM_OPERATION: &str =
    "rumi_provider_registry_pack.provider-registry-resource.stream";

pub const MANAGE_FUNCTION_ID: &str = "rumi_provider_registry_pack.provider-registry.manage";
pub const MANAGE_CONTRACT_ID: &str = "tobkiri.action.ai.provider.registry.manage.v1";
pub const MANAGE_OPERATION: &str = "rumi_provider_registry_pack.provider-registry-manage";

const SAVE_PAYLOAD_FIELDS: &[&str] = &["operation", "profile_id", "record", "expected_revision"];
const MODEL_ACCESS_PAYLOAD_FIELDS: &[&str] = &[
    "operation",
    "profile_id",
    "provider_instance_id",
    "expected_revision",
    "model_access",
];
const DELETE_PAYLOAD_FIELDS: &[&str] = &[
    "operation",
    "profile_id",
    "provider_instance_id",
    "expected_revision",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FactoryMode {
    Readonly,
    Manage,
}

impl FactoryMode {
    fn function_id(self) -> &'static str {
        match self {
            Self::Readonly => READONLY_FUNCTION_ID,
            Self::Manage => MANAGE_FUNCTION_ID,
        }
    }

    fn contract_id(self) -> &'static str {
        match self {
            Self::Readonly => READONLY_CONTRACT_ID,
            Self::Manage => MANAGE_CONTRACT_ID,
        }
    }

    fn accepts_operation(self, operation_id: &str) -> bool {
        match self {
            Self::Readonly => matches!(
                operation_id,
                READONLY_RESOURCE_OPERATION
                    | READONLY_GENERATE_OPERATION
                    | READONLY_STREAM_OPERATION
            ),
            Self::Manage => operation_id == MANAGE_OPERATION,
        }
    }
}

/// The minimal identity facts used by the original capture predicate.
///
/// This data is intentionally insufficient to construct a Host contribution:
/// digests, callable closures, contracts clients, and context are not emitted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CaptureBindingFacts<'a> {
    pub function_id: &'a str,
    pub contract_id: &'a str,
    pub operation_id: &'a str,
    /// True means the original domain lookup returned a value. An empty string
    /// is still present, matching the Python `domain_id is None` check.
    pub domain_is_present: bool,
}

/// Validate the source capture predicate and return its contribution count.
///
/// The caller supplies identity facts from a real Host context. This helper
/// never manufactures or stores those identities and therefore cannot serve
/// as capture authority or a production dispatcher.
pub fn validate_capture_facts(
    mode: FactoryMode,
    user_data_root_is_present: bool,
    profile_id: &PyValue,
    bindings: &[CaptureBindingFacts<'_>],
) -> SourceResult<usize> {
    if !user_data_root_is_present
        || !pytruthy(profile_id)
        || bindings.is_empty()
        || bindings.iter().any(|binding| {
            binding.function_id != mode.function_id()
                || binding.contract_id != mode.contract_id()
                || !mode.accepts_operation(binding.operation_id)
        })
    {
        return Err(SourceError::permission(
            "provider registry bindings are incomplete",
        ));
    }
    if bindings.iter().any(|binding| !binding.domain_is_present) {
        return Err(SourceError::permission(
            "provider registry domain binding is unavailable",
        ));
    }
    Ok(bindings.len())
}

/// Invoke one of the three captured readonly resource operations.
///
/// `.generate` and `.stream` are raw same-profile snapshot readers. The base
/// resource operation alone receives the UI-safe projection.
pub fn invoke_readonly<S: DocumentStore, C: Clock>(
    registry: &mut Registry<S, C>,
    captured_profile_id: &str,
    operation_id: &str,
    payload: &PyValue,
) -> SourceResult<PyValue> {
    if !FactoryMode::Readonly.accepts_operation(operation_id) {
        return Err(invalid_request_binding());
    }
    let fields = payload_object(payload)?;
    if request_profile(payload, captured_profile_id).is_err()
        || !validate_profile_id(&PyValue::String(captured_profile_id.into()))
            .is_ok_and(|profile| registry.profile_id() == profile)
    {
        return Err(invalid_request_binding());
    }
    if fields.iter().any(|(key, _)| key != "profile_id") {
        return Err(SourceError::permission(
            "provider registry read payload is invalid",
        ));
    }
    let snapshot = registry.snapshot()?;
    if operation_id == READONLY_RESOURCE_OPERATION {
        provider_connection_snapshot(&snapshot)
    } else {
        Ok(snapshot)
    }
}

/// Invoke the captured manage operation for its three allowed mutations.
///
/// Field and record-shape checks follow the source order: action selection,
/// record/policy mapping validation, record-key validation, payload-key and
/// exact revision validation, then the registry operation itself.
pub fn invoke_manage<S: DocumentStore, C: Clock>(
    registry: &mut Registry<S, C>,
    captured_profile_id: &str,
    operation_id: &str,
    payload: &PyValue,
) -> SourceResult<PyValue> {
    if operation_id != MANAGE_OPERATION {
        return Err(invalid_request_binding());
    }
    let fields = payload_object(payload)?;
    if request_profile(payload, captured_profile_id).is_err()
        || !validate_profile_id(&PyValue::String(captured_profile_id.into()))
            .is_ok_and(|profile| registry.profile_id() == profile)
    {
        return Err(invalid_request_binding());
    }
    let action = payload.get("operation").and_then(PyValue::as_str);
    let (allowed_payload_fields, record, policy) = match action {
        Some("save") => {
            let record = payload
                .get("record")
                .filter(|value| value.as_object().is_some());
            let Some(record) = record else {
                return Err(SourceError::value("provider registry record is required"));
            };
            if record.as_object().is_some_and(|entries| {
                entries.iter().any(|(key, _)| {
                    !matches!(
                        key.as_str(),
                        "provider_instance_id"
                            | "adapter_id"
                            | "display_name"
                            | "credential_handle"
                            | "endpoint"
                            | "enabled"
                            | "data_residency"
                            | "metadata"
                            | "model_access"
                    )
                })
            }) {
                return Err(SourceError::permission(
                    "provider registry record fields are invalid",
                ));
            }
            (SAVE_PAYLOAD_FIELDS, Some(record), None)
        }
        Some("model_access.save") => {
            let policy = payload
                .get("model_access")
                .filter(|value| value.as_object().is_some());
            let Some(policy) = policy else {
                return Err(SourceError::value("model access policy is required"));
            };
            (MODEL_ACCESS_PAYLOAD_FIELDS, None, Some(policy))
        }
        Some("delete") => (DELETE_PAYLOAD_FIELDS, None, None),
        _ => {
            return Err(SourceError::permission(
                "provider registry mutation is not permitted",
            ));
        }
    };

    if fields
        .iter()
        .any(|(key, _)| !allowed_payload_fields.contains(&key.as_str()))
        || !is_nonnegative_exact_int(payload.get("expected_revision"))
    {
        return Err(SourceError::permission(
            "provider registry mutation payload is invalid",
        ));
    }
    let revision = payload
        .get("expected_revision")
        .expect("strict revision validation requires a field");
    match action.expect("recognized action") {
        "save" => registry.save(record.expect("save record was validated"), revision),
        "model_access.save" => registry.set_model_access(
            get(payload, "provider_instance_id"),
            policy.expect("model access policy was validated"),
            revision,
        ),
        "delete" => registry.delete(get(payload, "provider_instance_id"), revision),
        _ => unreachable!("action selection is exhaustive"),
    }
}

/// The original redacted UI projection. Raw readers must use `snapshot()`.
pub fn provider_connection_snapshot(snapshot: &PyValue) -> SourceResult<PyValue> {
    if snapshot.as_object().is_none() {
        return Err(SourceError::new(
            "AttributeError",
            format!(
                "'{}' object has no attribute 'get'",
                source_type_name(snapshot)
            ),
        ));
    }
    let revision = get(snapshot, "revision");
    let providers = get(snapshot, "providers");
    if !is_nonnegative_exact_int(Some(revision)) || providers.as_array().is_none() {
        return Err(SourceError::value("provider registry snapshot is invalid"));
    }
    let revision = revision.clone();
    let mut projected = Vec::new();
    for item in providers.as_array().expect("validated providers list") {
        if item.as_object().is_none() {
            return Err(SourceError::value("provider registry record is invalid"));
        }
        let provider_id = get(item, "provider_instance_id");
        let display_name = get(item, "display_name");
        let enabled = get(item, "enabled");
        let credential_handle = get(item, "credential_handle");
        let adapter_id = get(item, "adapter_id");
        let evidence = item
            .get("health_evidence")
            .filter(|value| value.as_object().is_some());
        let verified = evidence
            .and_then(|value| value.get("verified"))
            .is_some_and(|value| matches!(value, PyValue::Bool(true)));
        let status = evidence
            .and_then(|value| value.get("status"))
            .unwrap_or(&NONE);
        let reachability = if !verified {
            "unknown"
        } else {
            if matches!(status, PyValue::Array(_) | PyValue::Object(_)) {
                return Err(SourceError::type_error(format!(
                    "unhashable type: '{}'",
                    source_type_name(status)
                )));
            }
            match status.as_str() {
                Some("available") => "available",
                Some("unavailable") => "unavailable",
                _ => "unknown",
            }
        };
        let observed_at = if verified {
            evidence
                .and_then(|value| value.get("observed_at"))
                .map(project_observed_at)
                .unwrap_or(PyValue::Null)
        } else {
            PyValue::Null
        };
        let credential_status = if adapter_id.as_str() == Some("local-openai-compatible")
            && matches!(credential_handle, PyValue::Null)
        {
            "not_required"
        } else if matches!(credential_handle, PyValue::String(value) if !value.is_empty()) {
            "configured"
        } else {
            "missing"
        };

        if !matches!(provider_id, PyValue::String(value) if !value.is_empty())
            || !matches!(display_name, PyValue::String(value) if !value.is_empty())
            || !matches!(enabled, PyValue::Bool(_))
        {
            return Err(SourceError::value("provider registry record is invalid"));
        }

        let mut projection = PyValue::object([
            ("provider_instance_id".into(), provider_id.clone()),
            ("display_name".into(), display_name.clone()),
            ("enabled".into(), enabled.clone()),
            (
                "credential_status".into(),
                PyValue::String(credential_status.into()),
            ),
            (
                "health_status".into(),
                PyValue::String(if verified { "verified" } else { "unverified" }.into()),
            ),
            ("reachability".into(), PyValue::String(reachability.into())),
            ("observed_at".into(), observed_at),
        ]);
        if let PyValue::String(value) = adapter_id
            && !pystrip(value).is_empty()
        {
            object_put(&mut projection, "adapter_id", adapter_id.clone());
        }
        let catalog_id = item
            .get("metadata")
            .filter(|value| value.as_object().is_some())
            .and_then(|metadata| metadata.get("catalog_provider_id"));
        if let Some(PyValue::String(value)) = catalog_id
            && !value.is_empty()
        {
            object_put(
                &mut projection,
                "catalog_provider_id",
                PyValue::String(value.clone()),
            );
        }
        projected.push(projection);
    }
    Ok(PyValue::object([
        ("revision".into(), revision),
        ("providers".into(), PyValue::Array(projected)),
    ]))
}

fn request_profile(payload: &PyValue, captured_profile_id: &str) -> SourceResult<()> {
    match payload.get("profile_id") {
        None => Ok(()),
        Some(PyValue::String(value)) if value == captured_profile_id => Ok(()),
        Some(_) => Err(invalid_request_binding()),
    }
}

fn invalid_request_binding() -> SourceError {
    SourceError::permission("provider registry request binding is invalid")
}

fn payload_object(payload: &PyValue) -> SourceResult<&[(String, PyValue)]> {
    payload.as_object().ok_or_else(|| {
        SourceError::new(
            "AttributeError",
            format!(
                "'{}' object has no attribute 'get'",
                source_type_name(payload)
            ),
        )
    })
}

fn source_type_name(value: &PyValue) -> &'static str {
    match value {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::String(_) => "str",
        PyValue::Array(_) => "list",
        PyValue::Object(_) => "dict",
    }
}

fn is_nonnegative_exact_int(value: Option<&PyValue>) -> bool {
    matches!(value, Some(PyValue::Int(digits)) if !digits.starts_with('-'))
}

fn project_observed_at(value: &PyValue) -> PyValue {
    const MAX_SAFE: u64 = 9_007_199_254_740_991;
    match value {
        PyValue::Int(digits) if !digits.starts_with('-') => digits
            .parse::<u64>()
            .ok()
            .filter(|number| *number <= MAX_SAFE)
            .map_or(PyValue::Null, |number| PyValue::Int(number.to_string())),
        PyValue::Float(number)
            if number.is_finite() && *number >= 0.0 && *number <= MAX_SAFE as f64 =>
        {
            PyValue::Int((number.floor() as u64).to_string())
        }
        _ => PyValue::Null,
    }
}

fn object_put(value: &mut PyValue, key: &str, replacement: PyValue) {
    let PyValue::Object(entries) = value else {
        unreachable!("projection is a dictionary")
    };
    entries.push((key.into(), replacement));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::MemoryStore;

    fn text(value: &str) -> PyValue {
        PyValue::String(value.into())
    }

    fn snapshot_with_record(record: PyValue) -> PyValue {
        PyValue::object([
            ("revision".into(), PyValue::Int("0".into())),
            ("providers".into(), PyValue::Array(vec![record])),
        ])
    }

    fn invalid_identity_record(status: PyValue) -> PyValue {
        PyValue::object([
            ("provider_instance_id".into(), PyValue::Null),
            ("display_name".into(), text("")),
            ("enabled".into(), PyValue::Bool(true)),
            (
                "health_evidence".into(),
                PyValue::object([
                    ("verified".into(), PyValue::Bool(true)),
                    ("status".into(), status),
                ]),
            ),
        ])
    }

    #[test]
    fn verified_unhashable_health_status_precedes_invalid_identity_fields() {
        for (status, type_name) in [
            (PyValue::Array(Vec::new()), "list"),
            (PyValue::Object(Vec::new()), "dict"),
        ] {
            let error = provider_connection_snapshot(&snapshot_with_record(
                invalid_identity_record(status),
            ))
            .unwrap_err();
            assert_eq!(error.exception_class, "TypeError");
            assert_eq!(error.message, format!("unhashable type: '{type_name}'"));
        }
    }

    #[test]
    fn non_mapping_payload_keeps_the_source_get_attribute_error() {
        let profile = text("defaults");
        let mut registry = Registry::new(&profile, MemoryStore::absent(), || {
            Ok("2026-01-01T00:00:00Z".into())
        })
        .unwrap();
        let error = invoke_readonly(
            &mut registry,
            "defaults",
            READONLY_RESOURCE_OPERATION,
            &PyValue::Null,
        )
        .unwrap_err();
        assert_eq!(error.exception_class, "AttributeError");
        assert_eq!(error.message, "'NoneType' object has no attribute 'get'");
    }

    #[test]
    fn non_mapping_projection_keeps_the_source_get_attribute_error() {
        let error = provider_connection_snapshot(&PyValue::Null).unwrap_err();
        assert_eq!(error.exception_class, "AttributeError");
        assert_eq!(error.message, "'NoneType' object has no attribute 'get'");
    }

    #[test]
    fn registry_and_captured_profile_must_be_the_same_before_read_or_write() {
        let profile = text("defaults");
        let mut registry = Registry::new(&profile, MemoryStore::absent(), || {
            Ok("2026-01-01T00:00:00Z".into())
        })
        .unwrap();
        let error = invoke_readonly(
            &mut registry,
            "foreign",
            READONLY_RESOURCE_OPERATION,
            &PyValue::Object(Vec::new()),
        )
        .unwrap_err();
        assert_eq!(error.exception_class, "PermissionError");
        assert_eq!(
            error.message,
            "provider registry request binding is invalid"
        );
    }

    #[test]
    fn captured_profile_binding_uses_registry_normalized_identity() {
        let profile = text("defaults");
        let mut registry = Registry::new(&profile, MemoryStore::absent(), || {
            Ok("2026-01-01T00:00:00Z".into())
        })
        .unwrap();
        let snapshot = invoke_readonly(
            &mut registry,
            " defaults ",
            READONLY_GENERATE_OPERATION,
            &PyValue::Object(Vec::new()),
        )
        .unwrap();
        assert_eq!(snapshot.get("profile_id"), Some(&text("defaults")));
    }
}
