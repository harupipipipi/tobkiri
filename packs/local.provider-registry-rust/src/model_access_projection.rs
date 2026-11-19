//! Public Model Access projections from the authorized Registry snapshot.

use crate::{
    failure::{SourceError, SourceResult},
    record::effective_model_access,
    state::{get, object, put, required, text},
};
use ordered_python_compat::{PyValue, pystr, pystrip, pytruthy};
use provider_model_access::registry_support::{
    NATIVE_FILTER_REVISION, hashable, is_official_openrouter, mapping, normalize_native_filters,
    type_name,
};

/// Project a public response or prepare the limited nested catalog request.
///
/// The caller supplies the authorized Registry snapshot independently of the
/// request and owns the catalog dispatch and the Registry freshness check.
pub fn project(snapshot: &PyValue, request: &PyValue) -> SourceResult<PyValue> {
    let (identity, phase) = parse_request(request)?;
    let connection = connection(snapshot, identity)?;
    let official = is_official_openrouter(connection)?;
    match phase {
        Phase::Policy => project_policy(snapshot, connection, identity, official),
        Phase::PrepareCatalog(discovery) => prepare_catalog(connection, official, discovery),
        Phase::ProjectCatalog(catalog) => project_catalog(snapshot, connection, identity, catalog),
    }
}

enum Phase<'a> {
    Policy,
    PrepareCatalog(Option<&'a PyValue>),
    ProjectCatalog(&'a PyValue),
}

fn parse_request(request: &PyValue) -> SourceResult<(&str, Phase<'_>)> {
    let invalid = || SourceError::permission("model access payload is invalid");
    let fields = request.as_object().ok_or_else(invalid)?;
    let identity = get(request, "provider_instance_id")
        .as_str()
        .filter(|identity| !pystrip(identity).is_empty())
        .ok_or_else(invalid)?;
    let (phase, extra_field) = match get(request, "phase").as_str() {
        Some("policy") => (Phase::Policy, None),
        Some("prepare-catalog") => (
            Phase::PrepareCatalog(request.get("discovery_filters")),
            Some("discovery_filters"),
        ),
        Some("project-catalog") => (
            Phase::ProjectCatalog(request.get("catalog").ok_or_else(invalid)?),
            Some("catalog"),
        ),
        _ => return Err(invalid()),
    };
    if fields.len() > 2 + usize::from(extra_field.is_some())
        || fields.iter().enumerate().any(|(index, (name, _))| {
            (!["phase", "provider_instance_id"].contains(&name.as_str())
                && Some(name.as_str()) != extra_field)
                || fields[..index].iter().any(|(previous, _)| previous == name)
        })
    {
        return Err(invalid());
    }
    Ok((identity, phase))
}

fn connection<'a>(snapshot: &'a PyValue, identity: &str) -> SourceResult<&'a PyValue> {
    let providers = get(snapshot, "providers")
        .as_array()
        .ok_or_else(|| SourceError::permission("provider registry snapshot is invalid"))?;
    let mut matches = providers
        .iter()
        .filter(|provider| get(provider, "provider_instance_id").as_str() == Some(identity));
    let unavailable = || SourceError::permission("provider connection is unavailable");
    let selected = matches.next().ok_or_else(unavailable)?;
    if !matches!(get(selected, "enabled"), PyValue::Bool(true)) || matches.next().is_some() {
        return Err(unavailable());
    }
    Ok(selected)
}

fn project_policy(
    snapshot: &PyValue,
    connection: &PyValue,
    identity: &str,
    official: bool,
) -> SourceResult<PyValue> {
    Ok(object([
        ("profile_id", required(snapshot, "profile_id")?.clone()),
        ("provider_instance_id", text(identity)),
        ("registry_revision", required(snapshot, "revision")?.clone()),
        ("model_access", effective_model_access(connection)?),
        (
            "native_capability",
            if official {
                text(NATIVE_FILTER_REVISION)
            } else {
                PyValue::Null
            },
        ),
    ]))
}

fn catalog_identity(connection: &PyValue) -> SourceResult<&str> {
    let provider = get(get(connection, "metadata"), "catalog_provider_id")
        .as_str()
        .filter(|provider| !provider.is_empty())
        .ok_or_else(|| SourceError::permission("provider catalog is unsupported"))?;
    let expected_adapter = match provider {
        "anthropic" => "anthropic",
        "ollama" | "lmstudio" | "vllm" | "llamacpp" => "local-openai-compatible",
        _ => "openai-compatible",
    };
    if get(connection, "adapter_id").as_str() != Some(expected_adapter) {
        return Err(SourceError::permission(
            "provider catalog adapter is incompatible",
        ));
    }
    Ok(provider)
}

fn prepare_catalog(
    connection: &PyValue,
    official: bool,
    discovery: Option<&PyValue>,
) -> SourceResult<PyValue> {
    let mut payload = object([("provider_id", text(catalog_identity(connection)?))]);
    if let Some(discovery) = discovery {
        let normalized = normalize_native_filters(
            &object([
                ("revision", text(NATIVE_FILTER_REVISION)),
                ("discovery", discovery.clone()),
                ("routing", object([])),
            ]),
            official,
        )?;
        put(
            &mut payload,
            "discovery_filters",
            get(&normalized, "discovery").clone(),
        );
    }
    Ok(payload)
}

fn project_catalog(
    snapshot: &PyValue,
    connection: &PyValue,
    identity: &str,
    catalog: &PyValue,
) -> SourceResult<PyValue> {
    let provider = catalog_identity(connection)?;
    let invalid = || SourceError::value("model catalog result is invalid");
    if catalog.as_object().is_none()
        || matches!(get(catalog, "success"), PyValue::Bool(false))
        || matches!(get(catalog, "ok"), PyValue::Bool(false))
        || matches!(get(catalog, "is_error"), PyValue::Bool(true))
    {
        return Err(invalid());
    }
    let status = get(catalog, "status");
    hashable(status)?;
    if matches!(status.as_str(), Some("failed" | "error" | "denied")) {
        return Err(invalid());
    }
    let models = get(catalog, "models").as_array().ok_or_else(invalid)?;

    let profile = required(snapshot, "profile_id")?;
    // The original catalog projection also validates the effective policy on
    // the saved connection before inspecting the catalog inventory.
    required(snapshot, "revision")?;
    effective_model_access(connection)?;
    let inventory = get(catalog, "inventory");
    let mut live = false;
    if pytruthy(inventory) {
        mapping(inventory)?;
        let entry = get(inventory, provider);
        if matches!(get(entry, "stale"), PyValue::Bool(false)) {
            let source = get(entry, "source");
            hashable(source)?;
            live = matches!(
                source.as_str(),
                Some("openrouter_models_api" | "openrouter_models_api_filtered")
            );
        }
    }
    if provider != "openrouter" {
        live = static_provider_supported(catalog, provider)?;
    }
    let models = models
        .iter()
        .filter(|model| get(model, "provider_id").as_str() == Some(provider))
        .filter_map(|model| {
            let id = get(model, "provider_model_id")
                .as_str()
                .filter(|id| !id.is_empty())?;
            let name = get(model, "display_name");
            let name = if pytruthy(name) {
                text(pystr(name))
            } else {
                text(id)
            };
            Some(object([("model_id", text(id)), ("display_name", name)]))
        })
        .collect();
    Ok(object([
        ("profile_id", profile.clone()),
        ("provider_instance_id", text(identity)),
        ("models", PyValue::Array(models)),
        ("status", text(if live { "live" } else { "unavailable" })),
    ]))
}

fn static_provider_supported(catalog: &PyValue, provider: &str) -> SourceResult<bool> {
    match catalog.get("providers") {
        Some(PyValue::Array(providers)) => Ok(providers
            .iter()
            .any(|entry| get(entry, "provider_id").as_str() == Some(provider))),
        // The Python loop sees only non-mapping keys or characters here.
        None | Some(PyValue::Object(_) | PyValue::String(_)) => Ok(false),
        Some(value) => Err(SourceError::type_error(format!(
            "'{}' object is not iterable",
            type_name(value)
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ordered_python_compat::loads;

    fn json(value: &str) -> PyValue {
        loads(value).unwrap()
    }

    fn snapshot(provider: &str, adapter: &str, endpoint: &str) -> PyValue {
        object([
            ("profile_id", text("defaults")),
            ("revision", PyValue::Int("4".into())),
            (
                "providers",
                PyValue::Array(vec![object([
                    ("provider_instance_id", text("connection/work")),
                    ("adapter_id", text(adapter)),
                    ("endpoint", text(endpoint)),
                    ("credential_handle", text("credential:private")),
                    ("enabled", PyValue::Bool(true)),
                    (
                        "metadata",
                        object([("catalog_provider_id", text(provider))]),
                    ),
                    ("allowed_models", json(r#"["z", "a", "a"]"#)),
                ])]),
            ),
        ])
    }

    fn openrouter() -> PyValue {
        snapshot(
            "openrouter",
            "openai-compatible",
            "https://openrouter.ai/api/v1",
        )
    }

    fn request(phase: &str) -> PyValue {
        object([
            ("phase", text(phase)),
            ("provider_instance_id", text("connection/work")),
        ])
    }

    fn catalog(snapshot: &PyValue, result: &str) -> SourceResult<PyValue> {
        let mut request = request("project-catalog");
        put(&mut request, "catalog", json(result));
        project(snapshot, &request)
    }

    #[test]
    fn policy_normalizes_legacy_ids_and_only_projects_public_fields() {
        assert_eq!(
            project(&openrouter(), &request("policy")).unwrap(),
            json(
                r#"{
                    "profile_id": "defaults",
                    "provider_instance_id": "connection/work",
                    "registry_revision": 4,
                    "model_access": {
                        "version": "tobkiri.connection-model-access.v1",
                        "mode": "explicit",
                        "model_ids": ["a", "z"]
                    },
                    "native_capability": "openrouter.official-filters.2026-10-07.v1"
                }"#
            )
        );
    }

    #[test]
    fn rejects_unknown_fields_and_missing_ambiguous_or_disabled_connections() {
        for request in [
            json(r#"{"phase":"policy","provider_instance_id":"connection/work","snapshot":{}}"#),
            json(r#"{"phase":"policy","provider_instance_id":"connection/work","catalog":{}}"#),
            json(r#"{"phase":"project-catalog","provider_instance_id":"connection/work"}"#),
            json(r#"{"phase":"prepare-catalog","provider_instance_id":"   "}"#),
            json(r#"{"phase":"policy","provider_instance_id":"connection/deleted"}"#),
        ] {
            assert_eq!(
                project(&openrouter(), &request)
                    .unwrap_err()
                    .exception_class,
                "PermissionError"
            );
        }
        let mut snapshot = openrouter();
        let mut connection = get(&snapshot, "providers").as_array().unwrap()[0].clone();
        put(
            &mut snapshot,
            "providers",
            PyValue::Array(vec![connection.clone(), connection.clone()]),
        );
        assert!(project(&snapshot, &request("policy")).is_err());
        for enabled in [PyValue::Bool(false), PyValue::Int("1".into())] {
            put(&mut connection, "enabled", enabled);
            put(
                &mut snapshot,
                "providers",
                PyValue::Array(vec![connection.clone()]),
            );
            assert!(project(&snapshot, &request("policy")).is_err());
        }
    }

    #[test]
    fn discovery_filters_are_normalized_only_for_official_openrouter() {
        let mut prepare = request("prepare-catalog");
        assert_eq!(
            project(&openrouter(), &prepare).unwrap(),
            json(r#"{"provider_id":"openrouter"}"#)
        );
        put(
            &mut prepare,
            "discovery_filters",
            json(r#"{"output_modalities":["image","image"],"category":"science"}"#),
        );
        assert_eq!(
            project(&openrouter(), &prepare).unwrap(),
            json(
                r#"{
                    "provider_id":"openrouter",
                    "discovery_filters":{"output_modalities":["image"],"category":"science"}
                }"#
            )
        );
        let lookalike = snapshot(
            "openrouter",
            "openai-compatible",
            "https://foreign.example/api/v1",
        );
        assert!(matches!(
            get(
                &project(&lookalike, &request("policy")).unwrap(),
                "native_capability"
            ),
            PyValue::Null
        ));
        assert!(project(&lookalike, &prepare).is_err());
        put(
            &mut prepare,
            "discovery_filters",
            json(r#"{"api_key":"private"}"#),
        );
        assert!(project(&openrouter(), &prepare).is_err());
        put(&mut prepare, "discovery_filters", PyValue::Null);
        assert!(project(&openrouter(), &prepare).is_err());
        let incompatible = snapshot("ollama", "openai-compatible", "https://foreign.example");
        assert!(project(&incompatible, &request("prepare-catalog")).is_err());
    }

    #[test]
    fn catalog_drops_foreign_models_and_projects_display_names_like_python() {
        assert_eq!(
            catalog(
                &openrouter(),
                r#"{
                    "credential_handle":"credential:private",
                    "inventory":{"openrouter":{"source":"openrouter_models_api_filtered","stale":false}},
                    "models":[
                        {"provider_id":"openai","provider_model_id":"foreign"},
                        {"provider_id":"openrouter","provider_model_id":""},
                        {"provider_id":"openrouter","provider_model_id":1},
                        false,
                        {"provider_id":"openrouter","provider_model_id":"model","display_name":0,"api_key":"private"},
                        {"provider_id":"openrouter","provider_model_id":"model-2","display_name":true},
                        {"provider_id":"openrouter","provider_model_id":"model-3","display_name":["label",7]}
                    ]
                }"#
            ).unwrap(),
            json(
                r#"{
                    "profile_id":"defaults",
                    "provider_instance_id":"connection/work",
                    "models":[
                        {"model_id":"model","display_name":"model"},
                        {"model_id":"model-2","display_name":"True"},
                        {"model_id":"model-3","display_name":"['label', 7]"}
                    ],
                    "status":"live"
                }"#
            )
        );
    }

    #[test]
    fn catalog_status_tracks_openrouter_freshness_or_static_provider_membership() {
        let stale = r#"{
            "models":[],
            "providers":[{"provider_id":"openrouter"},{"provider_id":"openai"}],
            "inventory":{"openrouter":{"source":"openrouter_models_api","stale":true}}
        }"#;
        let openai = snapshot("openai", "openai-compatible", "https://api.openai.com/v1");
        assert_eq!(
            get(&catalog(&openrouter(), stale).unwrap(), "status"),
            &text("unavailable")
        );
        assert_eq!(
            get(&catalog(&openai, stale).unwrap(), "status"),
            &text("live")
        );
        assert_eq!(
            get(&catalog(&openai, r#"{"models":[]}"#).unwrap(), "status"),
            &text("unavailable")
        );
        for malformed in [
            "[]",
            "{}",
            r#"{"models":{}}"#,
            r#"{"success":false,"models":[]}"#,
            r#"{"ok":false,"models":[]}"#,
            r#"{"is_error":true,"models":[]}"#,
            r#"{"status":"denied","models":[]}"#,
            r#"{"status":[],"models":[]}"#,
            r#"{"inventory":"unexpected","models":[]}"#,
        ] {
            assert!(catalog(&openrouter(), malformed).is_err(), "{malformed}");
        }
    }
}
