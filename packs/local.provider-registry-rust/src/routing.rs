//! Resolve the minimal connection data needed by the local chat route.

use crate::{
    failure::{SourceError, SourceResult},
    record::{identifier, provider_endpoint},
    registry::Registry,
    state::{Clock, DocumentStore, get, object, text},
};
use ordered_python_compat::PyValue;

/// Resolve an exact provider instance ID against the bound Registry.
pub(crate) fn resolve_local<S: DocumentStore, C: Clock>(
    registry: &mut Registry<S, C>,
    provider: &str,
) -> SourceResult<PyValue> {
    if identifier(&text(provider))? != provider {
        return Err(SourceError::value(
            "provider registry identifier is invalid",
        ));
    }
    let snapshot = registry.snapshot()?;
    let mut matches = get(&snapshot, "providers")
        .as_array()
        .expect("snapshot providers list")
        .iter()
        .filter(|record| get(record, "provider_instance_id").as_str() == Some(provider));
    let record = matches
        .next()
        .ok_or_else(|| SourceError::key_error("provider instance is unknown"))?;
    let adapter = get(record, "adapter_id");
    let credential = get(record, "credential_handle");
    if matches.next().is_some()
        || adapter.as_str() != Some("local-openai-compatible")
        || get(record, "enabled") != &PyValue::Bool(true)
        || credential != &PyValue::Null
    {
        return Err(SourceError::permission(
            "local provider route is unavailable",
        ));
    }
    let endpoint = provider_endpoint(get(record, "endpoint"), credential, adapter)?;
    Ok(object([("base_url", endpoint)]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{MemoryStore, STORE_VERSION};

    const PROVIDER: &str = "connection/local";

    fn connection(adapter: &str, enabled: PyValue, endpoint: &str, credential: PyValue) -> PyValue {
        object([
            ("provider_instance_id", text(PROVIDER)),
            ("adapter_id", text(adapter)),
            ("display_name", text("local-provider")),
            ("enabled", enabled),
            ("endpoint", text(endpoint)),
            ("credential_handle", credential),
        ])
    }

    fn local(enabled: PyValue, endpoint: &str, credential: PyValue) -> PyValue {
        connection("local-openai-compatible", enabled, endpoint, credential)
    }

    fn registry(profile: &str, providers: PyValue) -> Registry<MemoryStore, impl Clock> {
        Registry::new(
            &text(profile),
            MemoryStore::initialized(object([
                ("version", text(STORE_VERSION)),
                ("profile_id", text("work")),
                ("revision", PyValue::Int("1".into())),
                ("providers", providers),
            ])),
            || panic!("route lookup must not use the clock"),
        )
        .unwrap()
    }

    #[test]
    fn resolves_only_the_normalized_loopback_base_url() {
        for endpoint in ["http://127.0.0.1:1234/v1", "http://[::1]:1234/v1"] {
            let record = local(PyValue::Bool(true), &format!("{endpoint}/"), PyValue::Null);
            let mut registry = registry("work", object([(PROVIDER, record)]));
            assert_eq!(
                resolve_local(&mut registry, PROVIDER).unwrap(),
                object([("base_url", text(endpoint))])
            );
            assert_eq!(registry.store().write_count(), 0);
        }
    }

    #[test]
    fn rejects_unknown_and_ambiguous_provider_ids() {
        let record = local(
            PyValue::Bool(true),
            "http://127.0.0.1:1234/v1",
            PyValue::Null,
        );
        let duplicate = local(
            PyValue::Bool(false),
            "http://127.0.0.1:2345/v1",
            PyValue::Null,
        );
        let mut registry = registry(
            "work",
            object([(PROVIDER, record), ("duplicate", duplicate)]),
        );
        assert_eq!(
            resolve_local(&mut registry, "local-provider")
                .unwrap_err()
                .exception_class,
            "KeyError"
        );
        assert_eq!(
            resolve_local(&mut registry, " connection/local")
                .unwrap_err()
                .exception_class,
            "ValueError"
        );
        assert_eq!(
            resolve_local(&mut registry, PROVIDER)
                .unwrap_err()
                .exception_class,
            "PermissionError"
        );
    }

    #[test]
    fn rejects_disabled_remote_credentialed_and_unapproved_routes() {
        let endpoint = "http://127.0.0.1:1234/v1";
        for record in [
            local(PyValue::Bool(false), endpoint, PyValue::Null),
            local(text("true"), endpoint, PyValue::Null),
            connection(
                "openai-compatible",
                PyValue::Bool(true),
                endpoint,
                PyValue::Null,
            ),
            local(PyValue::Bool(true), endpoint, text("opaque:fixture")),
            local(
                PyValue::Bool(true),
                "http://example.test:1234/v1",
                PyValue::Null,
            ),
            local(
                PyValue::Bool(true),
                "http://127.0.0.1:1234/v1?x=1",
                PyValue::Null,
            ),
        ] {
            let mut registry = registry("work", object([(PROVIDER, record)]));
            assert!(resolve_local(&mut registry, PROVIDER).is_err());
        }
    }

    #[test]
    fn rejects_documents_from_another_profile() {
        let record = local(
            PyValue::Bool(true),
            "http://127.0.0.1:1234/v1",
            PyValue::Null,
        );
        let mut registry = registry("personal", object([(PROVIDER, record)]));
        assert_eq!(
            resolve_local(&mut registry, PROVIDER).unwrap_err().message,
            "provider registry profile binding is invalid"
        );
    }
}
