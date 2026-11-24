//! Provider configuration prepare/execute logic translated from the original
//! `runtime/configuration.py`.
//!
//! This module owns request validation, frozen-plan revalidation, Registry
//! compare-and-swap, and the source's narrow credential compensation rule. It
//! does not own the outer interactive-effect coordinator, operation identity,
//! retry/status/resume state, profile locking, or durable storage.

use crate::{
    failure::{SourceError, SourceResult},
    record,
    registry::Registry,
    state::{Clock, DocumentStore, get, object, put, text},
};
use ordered_python_compat::{PyValue, pystrip};
use provider_model_access::registry_support::split_url;
use sha2::{Digest as _, Sha256};
use std::borrow::Cow;
use zeroize::Zeroizing;

pub const CREDENTIAL_CONTRACT: &str = "tobkiri.action.credential.manage.v1";
pub const CREDENTIAL_OPERATION: &str = "rumi_credential_broker_pack.credential-manage";

const TEXT_SCOPES: [&str; 2] = ["ai.generate", "ai.stream"];
const CONFIGURATION_SCOPES: [&str; 4] = [
    "ai.generate",
    "ai.stream",
    "ai.audio.transcribe",
    "ai.audio.speech",
];

/// Borrowed credential-create input. The bridge should serialize this request
/// into the existing GlobalContract operation without retaining the API key.
#[derive(Clone, Copy)]
pub struct CredentialCreate<'a> {
    pub profile_id: &'a str,
    pub api_key: &'a str,
    pub consumer_pack_id: &'a str,
    pub provider_instance_id: &'a str,
    pub scopes: &'a [&'a str],
}

/// The original client accepted a mapping and then validated each returned
/// field. `None` retains the same rejection path for missing or non-string
/// response fields without exposing arbitrary response data to this module.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct CredentialCreateAck {
    pub handle: Option<String>,
    pub profile_id: Option<String>,
    pub consumer_pack_id: Option<String>,
    pub provider_instance_id: Option<String>,
}

/// Narrow adapter for the existing Credential Broker contract.
///
/// Every create/revoke adapter error is deliberately opaque here: the source
/// maps all such exceptions to fixed RuntimeError messages and never retries
/// an unacknowledged create. The active interactive-effect owner remains
/// responsible for the operation identity and any retry/status lifecycle.
pub trait CredentialPort {
    type Error;

    fn create(&mut self, request: CredentialCreate<'_>)
    -> Result<CredentialCreateAck, Self::Error>;

    fn revoke(&mut self, profile_id: &str, handle: &str) -> Result<(), Self::Error>;
}

struct Request<'a> {
    connection_name: &'a str,
    protocol: &'a str,
    endpoint: Cow<'a, str>,
    submitted_endpoint: &'a str,
    key_value: &'a str,
    catalog_provider_id: Option<&'a str>,
    display_name: Option<&'a str>,
    capabilities: Option<Vec<&'a str>>,
    submitted_capabilities: Option<Vec<&'a str>>,
}

/// Validate provider setup data without accepting Profile or authority fields.
/// The raw request is never copied, so the API key does not acquire another
/// ordinary `String` allocation in the configuration owner.
fn configuration_request(payload: &PyValue) -> SourceResult<Request<'_>> {
    let Some(fields) = payload.as_object() else {
        return Err(SourceError::value(
            "provider configuration fields are invalid",
        ));
    };
    const REQUIRED: [&str; 4] = ["connection_name", "protocol", "endpoint", "key_value"];
    const OPTIONAL: [&str; 3] = ["catalog_provider_id", "display_name", "capabilities"];
    if fields.len() > REQUIRED.len() + OPTIONAL.len()
        || fields
            .iter()
            .enumerate()
            .any(|(index, (name, _))| fields[..index].iter().any(|(earlier, _)| earlier == name))
        || REQUIRED.iter().any(|name| payload.get(name).is_none())
        || fields.iter().any(|(name, _)| {
            !REQUIRED.contains(&name.as_str()) && !OPTIONAL.contains(&name.as_str())
        })
        || fields
            .iter()
            .any(|(name, value)| name != "capabilities" && !matches!(value, PyValue::String(_)))
    {
        return Err(SourceError::value(
            "provider configuration fields are invalid",
        ));
    }

    let connection_name = payload
        .get("connection_name")
        .and_then(PyValue::as_str)
        .expect("required string was validated");
    let protocol = payload
        .get("protocol")
        .and_then(PyValue::as_str)
        .expect("required string was validated");
    let endpoint = payload
        .get("endpoint")
        .and_then(PyValue::as_str)
        .expect("required string was validated");
    let key_value = payload
        .get("key_value")
        .and_then(PyValue::as_str)
        .expect("required string was validated");
    let catalog_provider_id = payload.get("catalog_provider_id").and_then(PyValue::as_str);
    let display_name = payload.get("display_name").and_then(PyValue::as_str);

    if catalog_provider_id.is_some_and(|value| !valid_name(value))
        || display_name.is_some_and(|value| {
            pystrip(value).is_empty()
                || value.chars().count() > 200
                || value.chars().any(is_control)
        })
    {
        return Err(SourceError::value(
            "provider catalog identity or display name is invalid",
        ));
    }

    let (submitted_capabilities, capabilities) = match payload.get("capabilities") {
        None => (None, None),
        Some(value) => {
            let Some(values) = value.as_array() else {
                return Err(SourceError::value(
                    "provider configuration capabilities are invalid",
                ));
            };
            if values.is_empty() || values.len() > CONFIGURATION_SCOPES.len() {
                return Err(SourceError::value(
                    "provider configuration capabilities are invalid",
                ));
            }
            let mut scopes = Vec::with_capacity(values.len());
            for item in values {
                let Some(scope) = item.as_str() else {
                    return Err(SourceError::value(
                        "provider configuration capabilities are invalid",
                    ));
                };
                if !CONFIGURATION_SCOPES.contains(&scope)
                    || (protocol != "openai-compatible" && !TEXT_SCOPES.contains(&scope))
                    || scopes.contains(&scope)
                {
                    return Err(SourceError::value(
                        "provider configuration capabilities are invalid",
                    ));
                }
                scopes.push(scope);
            }
            let submitted = scopes.clone();
            scopes.sort_unstable();
            (Some(submitted), Some(scopes))
        }
    };

    if !valid_name(connection_name)
        || !matches!(
            protocol,
            "openai-compatible" | "anthropic" | "local-openai-compatible"
        )
        || key_value.chars().count() > 16_384
        || key_value.chars().any(is_control)
        || endpoint.chars().count() > 2_048
    {
        return Err(SourceError::value(
            "provider configuration values are invalid",
        ));
    }

    if protocol == "local-openai-compatible" {
        if !key_value.is_empty() {
            return Err(SourceError::value(
                "local provider credentials are not permitted",
            ));
        }
        let canonical =
            record::provider_endpoint(&text(endpoint), &PyValue::Null, &text(protocol))?;
        let canonical = canonical
            .into_string()
            .expect("local endpoint normalizer returns a string");
        return Ok(Request {
            connection_name,
            protocol,
            endpoint: Cow::Owned(canonical),
            submitted_endpoint: endpoint,
            key_value,
            catalog_provider_id,
            display_name,
            capabilities,
            submitted_capabilities,
        });
    }

    if key_value.is_empty() {
        return Err(SourceError::value(
            "provider configuration values are invalid",
        ));
    }
    let parsed =
        split_url(endpoint).map_err(|_| SourceError::value("provider endpoint is invalid"))?;
    let port = parsed
        .port()
        .map_err(|_| SourceError::value("provider endpoint is invalid"))?;
    if parsed.scheme != "https"
        || parsed.hostname.is_none()
        || parsed.userinfo
        || !parsed.query.is_empty()
        || !parsed.fragment.is_empty()
        || port.is_some_and(|port| !(1..=65_535).contains(&port))
        || endpoint
            .chars()
            .any(|character| pystrip(&character.to_string()).is_empty())
        || endpoint.contains(key_value)
        || connection_name.contains(key_value)
        || display_name.is_some_and(|value| value.contains(key_value))
        || catalog_provider_id.is_some_and(|value| value.contains(key_value))
    {
        return Err(SourceError::value("provider endpoint is invalid"));
    }

    Ok(Request {
        connection_name,
        protocol,
        endpoint: Cow::Borrowed(endpoint),
        submitted_endpoint: endpoint,
        key_value,
        catalog_provider_id,
        display_name,
        capabilities,
        submitted_capabilities,
    })
}

/// Build the source's frozen plan and preflight the proposed Registry record.
/// The digest binds the original, unnormalized request, including local URL
/// spelling and the API key; it is never returned as secret material.
pub fn prepare_configuration<S: DocumentStore, C: Clock>(
    registry: &mut Registry<S, C>,
    payload: &PyValue,
) -> SourceResult<PyValue> {
    let request = configuration_request(payload)?;
    let snapshot = registry.snapshot()?;
    let expected_revision = snapshot
        .get("revision")
        .expect("Registry snapshot contains revision")
        .clone();
    // Match the source order: validate/normalize first, read the current
    // Registry revision second, then digest the original request spelling.
    let request_digest = canonical_digest(&request)?;
    let mut plan = object([
        ("profile_id", text(registry.profile_id())),
        (
            "provider_instance_id",
            text(format!("provider.{}", request.connection_name)),
        ),
        ("adapter_id", text(request.protocol)),
        ("endpoint", text(request.endpoint.as_ref())),
        ("expected_revision", expected_revision.clone()),
        ("request_digest", text(request_digest)),
    ]);
    if let Some(scopes) = &request.capabilities {
        put(
            &mut plan,
            "credential_scopes",
            PyValue::Array(scopes.iter().map(|scope| text(*scope)).collect()),
        );
    }
    let record = connection_record(&request, &plan, PyValue::Null);
    registry.prepare_save(&record, &expected_revision)?;
    Ok(plan)
}

/// Execute one frozen provider setup. A failed/ambiguous credential create is
/// never retried or compensated; only a new handle proven absent from a
/// readable Registry snapshot may be revoked after a failed Registry save.
pub fn execute_configuration<S: DocumentStore, C: Clock, P: CredentialPort>(
    registry: &mut Registry<S, C>,
    credentials: &mut P,
    payload: &PyValue,
    consumer_pack_id: &str,
) -> SourceResult<PyValue> {
    let Some(fields) = payload.as_object() else {
        return Err(SourceError::value(
            "provider configuration execution is invalid",
        ));
    };
    if fields.len() != 2 || payload.get("request").is_none() || payload.get("plan").is_none() {
        return Err(SourceError::value(
            "provider configuration execution is invalid",
        ));
    }
    let request_payload = payload.get("request").expect("request key was checked");
    let supplied_plan = payload.get("plan").expect("plan key was checked");
    if request_payload.as_object().is_none() || supplied_plan.as_object().is_none() {
        return Err(SourceError::value(
            "provider configuration execution is invalid",
        ));
    }

    if !python_equal(
        supplied_plan,
        &prepare_configuration(registry, request_payload)?,
    ) {
        return Err(SourceError::permission(
            "provider configuration changed after preparation",
        ));
    }
    let request = configuration_request(request_payload)?;
    let expected_revision = supplied_plan
        .get("expected_revision")
        .expect("a matching plan contains expected_revision");
    let prepared = registry.prepare_save(
        &connection_record(&request, supplied_plan, PyValue::Null),
        expected_revision,
    )?;

    if request.protocol == "local-openai-compatible" {
        match registry.save(&prepared, expected_revision) {
            Ok(_) => return Ok(configured_result(&prepared)),
            Err(error) => {
                if is_known_registry_rejection(&error)
                    || !connection_save_confirmed(
                        &save_snapshot(registry)?,
                        &prepared,
                        expected_revision,
                    )?
                {
                    return Err(connection_save_error());
                }
                return Ok(configured_result(&prepared));
            }
        }
    }

    let default_scopes = TEXT_SCOPES;
    let scopes = match supplied_plan.get("credential_scopes") {
        Some(value) => value
            .as_array()
            .expect("matching plan has a scope list")
            .iter()
            .map(|scope| scope.as_str().expect("matching plan has string scopes"))
            .collect::<Vec<_>>(),
        None => default_scopes.to_vec(),
    };
    let created = credentials
        .create(CredentialCreate {
            profile_id: registry.profile_id(),
            api_key: request.key_value,
            consumer_pack_id,
            provider_instance_id: supplied_plan
                .get("provider_instance_id")
                .and_then(PyValue::as_str)
                .expect("matching plan has provider_instance_id"),
            scopes: &scopes,
        })
        .map_err(|_| credential_save_error())?;
    let Some(handle) = created
        .handle
        .as_deref()
        .filter(|handle| handle.starts_with("credential:"))
    else {
        return Err(credential_save_error());
    };
    if created.profile_id.as_deref() != Some(registry.profile_id())
        || created.provider_instance_id.as_deref()
            != supplied_plan
                .get("provider_instance_id")
                .and_then(PyValue::as_str)
        || created.consumer_pack_id.as_deref() != Some(consumer_pack_id)
    {
        return Err(credential_save_error());
    }

    let mut record = prepared;
    put(&mut record, "credential_handle", text(handle));
    match registry.save(&record, expected_revision) {
        Ok(_) => Ok(configured_result(&record)),
        Err(error) => {
            let snapshot = save_snapshot(registry)?;
            let confirmed = !is_known_registry_rejection(&error)
                && connection_save_confirmed(&snapshot, &record, expected_revision)?;
            if confirmed {
                return Ok(configured_result(&record));
            }
            if snapshot_references_handle(&snapshot, handle) {
                return Err(connection_save_error());
            }
            credentials
                .revoke(registry.profile_id(), handle)
                .map_err(|_| cleanup_error())?;
            Err(connection_save_error())
        }
    }
}

fn connection_record(request: &Request<'_>, plan: &PyValue, handle: PyValue) -> PyValue {
    let mut record = object([
        (
            "provider_instance_id",
            plan.get("provider_instance_id")
                .expect("configuration plan has provider id")
                .clone(),
        ),
        (
            "adapter_id",
            plan.get("adapter_id").expect("plan adapter").clone(),
        ),
        (
            "endpoint",
            plan.get("endpoint").expect("plan endpoint").clone(),
        ),
        ("credential_handle", handle),
    ]);
    if let Some(display_name) = request.display_name.filter(|value| !value.is_empty()) {
        put(&mut record, "display_name", text(display_name));
    }
    if let Some(catalog_provider_id) = request
        .catalog_provider_id
        .filter(|value| !value.is_empty())
    {
        put(
            &mut record,
            "metadata",
            object([("catalog_provider_id", text(catalog_provider_id))]),
        );
    }
    record
}

fn configured_result(record: &PyValue) -> PyValue {
    object([
        ("configured", PyValue::Bool(true)),
        (
            "provider_instance_id",
            record
                .get("provider_instance_id")
                .expect("saved record contains provider id")
                .clone(),
        ),
    ])
}

fn save_snapshot<S: DocumentStore, C: Clock>(
    registry: &mut Registry<S, C>,
) -> SourceResult<PyValue> {
    registry.snapshot().map_err(|_| connection_save_error())
}

fn connection_save_confirmed(
    snapshot: &PyValue,
    record: &PyValue,
    expected_revision: &PyValue,
) -> SourceResult<bool> {
    let expected_next = crate::state::increment_store_revision(expected_revision)?;
    if !python_equal(get(snapshot, "revision"), &expected_next) {
        return Ok(false);
    }
    let Some(providers) = get(snapshot, "providers").as_array() else {
        return Ok(false);
    };
    let provider_id = record
        .get("provider_instance_id")
        .and_then(PyValue::as_str)
        .expect("prepared record contains provider id");
    let matches = providers
        .iter()
        .filter(|item| {
            item.get("provider_instance_id").and_then(PyValue::as_str) == Some(provider_id)
        })
        .collect::<Vec<_>>();
    if matches.len() != 1 {
        return Ok(false);
    }
    let saved = matches[0];
    if !matches!(saved.get("created_at"), Some(PyValue::String(_)))
        || !matches!(saved.get("updated_at"), Some(PyValue::String(_)))
    {
        return Ok(false);
    }
    let Some(saved_fields) = saved.as_object() else {
        return Ok(false);
    };
    let saved_without_timestamps = PyValue::object(
        saved_fields
            .iter()
            .filter(|(key, _)| key != "created_at" && key != "updated_at")
            .cloned(),
    );
    Ok(python_equal(&saved_without_timestamps, record))
}

fn snapshot_references_handle(snapshot: &PyValue, handle: &str) -> bool {
    get(snapshot, "providers")
        .as_array()
        .is_some_and(|providers| {
            providers.iter().any(|provider| {
                provider.get("credential_handle").and_then(PyValue::as_str) == Some(handle)
            })
        })
}

fn is_known_registry_rejection(error: &SourceError) -> bool {
    matches!(
        error.exception_class,
        "ValueError" | "ProviderRegistryConflict"
    )
}

fn connection_save_error() -> SourceError {
    SourceError::new("RuntimeError", "provider connection save was not confirmed")
}

fn credential_save_error() -> SourceError {
    SourceError::new("RuntimeError", "provider credential save was not confirmed")
}

fn cleanup_error() -> SourceError {
    SourceError::new(
        "RuntimeError",
        "provider configuration cleanup was not confirmed",
    )
}

fn valid_name(value: &str) -> bool {
    let mut characters = value.chars();
    let Some(first) = characters.next() else {
        return false;
    };
    value.chars().count() <= 100
        && first.is_ascii_alphanumeric()
        && characters.all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
        })
}

fn is_control(character: char) -> bool {
    (character as u32) < 32 || character == '\u{7f}'
}

/// Python's mapping/list equality is order-insensitive for dictionaries and
/// numeric across bool/int/float, unlike `PyValue`'s structural equality.
fn python_equal(left: &PyValue, right: &PyValue) -> bool {
    if let (Some(left_number), Some(right_number)) = (numeric_integer(left), numeric_integer(right))
    {
        return left_number == right_number;
    }
    match (left, right) {
        (PyValue::Float(left), PyValue::Float(right)) => left == right,
        (PyValue::Null, PyValue::Null) => true,
        (PyValue::String(left), PyValue::String(right)) => left == right,
        (PyValue::Array(left), PyValue::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| python_equal(left, right))
        }
        (PyValue::Object(left), PyValue::Object(right)) => {
            left.len() == right.len()
                && unique_keys(left)
                && unique_keys(right)
                && left.iter().all(|(key, value)| {
                    right
                        .iter()
                        .find(|(candidate, _)| candidate == key)
                        .is_some_and(|(_, candidate)| python_equal(value, candidate))
                })
        }
        _ => false,
    }
}

// Raw wire values can represent duplicate keys that a Python dict cannot.
// Reject that invalid mapping instead of treating repeated matching fields as
// a complete frozen plan and reaching missing-field assumptions below.
fn unique_keys(fields: &[(String, PyValue)]) -> bool {
    fields
        .iter()
        .enumerate()
        .all(|(index, (name, _))| fields[..index].iter().all(|(previous, _)| previous != name))
}

fn numeric_integer(value: &PyValue) -> Option<String> {
    match value {
        PyValue::Bool(_) | PyValue::Int(_) => ordered_python_compat::pyint(value).ok(),
        PyValue::Float(value) if value.is_finite() && value.fract() == 0.0 => {
            if *value == 0.0 {
                Some("0".into())
            } else {
                ordered_python_compat::pyint(&PyValue::Float(*value)).ok()
            }
        }
        _ => None,
    }
}

#[derive(Clone, Copy)]
enum CanonicalField<'a> {
    String(&'a str),
    Strings(&'a [&'a str]),
}

fn canonical_string_len(value: &str) -> Option<usize> {
    value.chars().try_fold(2usize, |size, character| {
        size.checked_add(match character {
            '"' | '\\' | '\u{8}' | '\u{c}' | '\n' | '\r' | '\t' => 2,
            '\u{0}'..='\u{1f}' => 6,
            _ => character.len_utf8(),
        })
    })
}

fn canonical_field_len(field: CanonicalField<'_>) -> Option<usize> {
    match field {
        CanonicalField::String(value) => canonical_string_len(value),
        CanonicalField::Strings(values) => {
            let mut size = 2usize; // array brackets
            for (index, value) in values.iter().enumerate() {
                size = size.checked_add(canonical_string_len(value)?)?;
                if index != 0 {
                    size = size.checked_add(1)?;
                }
            }
            Some(size)
        }
    }
}

fn write_canonical_string(value: &str, output: &mut Vec<u8>) {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    output.push(b'"');
    for character in value.chars() {
        match character {
            '"' => output.extend_from_slice(b"\\\""),
            '\\' => output.extend_from_slice(b"\\\\"),
            '\u{8}' => output.extend_from_slice(b"\\b"),
            '\u{c}' => output.extend_from_slice(b"\\f"),
            '\n' => output.extend_from_slice(b"\\n"),
            '\r' => output.extend_from_slice(b"\\r"),
            '\t' => output.extend_from_slice(b"\\t"),
            '\u{0}'..='\u{1f}' => {
                let value = character as u8;
                output.extend_from_slice(b"\\u00");
                output.push(HEX[usize::from(value >> 4)]);
                output.push(HEX[usize::from(value & 0x0f)]);
            }
            _ => {
                let mut buffer = [0; 4];
                output.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
            }
        }
    }
    output.push(b'"');
}

fn write_canonical_field(field: CanonicalField<'_>, output: &mut Vec<u8>) {
    match field {
        CanonicalField::String(value) => write_canonical_string(value, output),
        CanonicalField::Strings(values) => {
            output.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    output.push(b',');
                }
                write_canonical_string(value, output);
            }
            output.push(b']');
        }
    }
}

/// Encode only the private, validated configuration shape. Its fixed keys are
/// already listed in Python code-point order; no arbitrary `PyValue` tree or
/// secret-bearing ordinary clone is created for request canonicalization.
fn canonical_bytes(request: &Request<'_>) -> SourceResult<Zeroizing<Vec<u8>>> {
    let fields = [
        (
            "capabilities",
            request
                .submitted_capabilities
                .as_deref()
                .map(CanonicalField::Strings),
        ),
        (
            "catalog_provider_id",
            request.catalog_provider_id.map(CanonicalField::String),
        ),
        (
            "connection_name",
            Some(CanonicalField::String(request.connection_name)),
        ),
        (
            "display_name",
            request.display_name.map(CanonicalField::String),
        ),
        (
            "endpoint",
            Some(CanonicalField::String(request.submitted_endpoint)),
        ),
        ("key_value", Some(CanonicalField::String(request.key_value))),
        ("protocol", Some(CanonicalField::String(request.protocol))),
    ];

    let mut size = 2usize; // opening and closing object braces
    let mut first = true;
    for (key, value) in fields {
        let Some(value) = value else {
            continue;
        };
        size = size
            .checked_add(if first { 0 } else { 1 })
            .and_then(|size| size.checked_add(canonical_string_len(key)?))
            .and_then(|size| size.checked_add(1)) // colon
            .and_then(|size| size.checked_add(canonical_field_len(value)?))
            .ok_or_else(|| {
                SourceError::new("CanonicalizationError", "canonical JSON exceeds size limit")
            })?;
        first = false;
    }
    if size > 4 * 1024 * 1024 {
        return Err(SourceError::new(
            "CanonicalizationError",
            "canonical JSON exceeds size limit",
        ));
    }

    // Compute the exact encoded length before allocating, then reserve that
    // amount once. The validated source limits bound this request well below
    // 128 KiB even with JSON escaping, so the secret-bearing buffer never has
    // to grow and leave an earlier allocation behind.
    let mut encoded = Zeroizing::new(Vec::new());
    encoded
        .try_reserve_exact(size)
        .map_err(|_| SourceError::new("MemoryError", ""))?;
    encoded.push(b'{');
    let mut first = true;
    for (key, value) in fields {
        let Some(value) = value else {
            continue;
        };
        if !first {
            encoded.push(b',');
        }
        first = false;
        write_canonical_string(key, &mut encoded);
        encoded.push(b':');
        write_canonical_field(value, &mut encoded);
    }
    encoded.push(b'}');
    debug_assert_eq!(encoded.len(), size);
    Ok(encoded)
}

fn canonical_digest(request: &Request<'_>) -> SourceResult<String> {
    let encoded = canonical_bytes(request)?;
    let hash = Sha256::digest(encoded.as_slice());
    Ok(format!("sha256:{hash:x}"))
}

#[cfg(test)]
#[path = "configuration_tests.rs"]
mod tests;
