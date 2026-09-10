//! Web entry selection from a verified Application map. No execution grants.

use std::collections::BTreeSet;

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct FrontendEntry {
    pub entry_id: String,
    pub route: String,
    #[serde(rename = "match")]
    pub route_match: String,
    pub contribution_id: String,
    pub implementation: String,
    pub label: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrontendEntries {
    default_entry_id: String,
    entries: Vec<FrontendEntry>,
}

/// A selected declaration and the exact map artifact that contains it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct VerifiedFrontendEntry {
    pub entry: FrontendEntry,
    pub map_digest: String,
}

/// Resolve an explicit Profile entry, or the map's explicit default.
/// The caller verifies the map bytes, manifest and Profile before calling.
pub(crate) fn resolve(
    map: &Value,
    map_digest: &str,
    requested_entry: Option<&Value>,
) -> Result<VerifiedFrontendEntry> {
    let raw = map
        .get("frontend")
        .context("Application has no frontend entries")?;
    if serde_json::to_vec(raw)?.len() > 64 * 1024 {
        bail!("Application frontend declaration exceeds its limit");
    }
    let declaration: FrontendEntries = serde_json::from_value(raw.clone())
        .context("Application frontend declaration is malformed")?;
    if declaration.entries.is_empty() || declaration.entries.len() > 64 {
        bail!("Application frontend entries are invalid");
    }
    let mut identities = BTreeSet::new();
    let mut contributions = BTreeSet::new();
    let mut routes = BTreeSet::new();
    for entry in &declaration.entries {
        if !valid_id(&entry.entry_id)
            || !valid_id(&entry.contribution_id)
            || !valid_id(&entry.implementation)
            || entry.label.trim().is_empty()
            || entry.label.chars().count() > 256
            || !matches!(entry.route_match.as_str(), "exact" | "subpath")
            || !valid_route(&entry.route)
        {
            bail!("Application frontend entry is invalid");
        }
        if !identities.insert(&entry.entry_id)
            || !contributions.insert(&entry.contribution_id)
            || !routes.insert(&entry.route)
        {
            bail!("Application frontend entry is ambiguous");
        }
    }
    for entry in &declaration.entries {
        let prefix = format!("{}/", entry.route.trim_end_matches('/'));
        if entry.route_match == "subpath"
            && routes
                .iter()
                .any(|route| **route != entry.route && route.starts_with(&prefix))
        {
            bail!("Application frontend routes overlap");
        }
    }
    if !identities.contains(&declaration.default_entry_id) {
        bail!("Application frontend default entry is missing");
    }
    let selected = match requested_entry {
        Some(value) => value
            .as_str()
            .context("Profile frontend entry ID is invalid")?,
        None => &declaration.default_entry_id,
    };
    let entry = declaration
        .entries
        .iter()
        .find(|entry| entry.entry_id == selected)
        .context("Profile frontend entry is not declared by the Application")?;
    Ok(VerifiedFrontendEntry {
        entry: entry.clone(),
        map_digest: map_digest.to_owned(),
    })
}

fn valid_id(value: &str) -> bool {
    value.len() <= 128
        && !["rumi.", "rumiai.", "viewer.", "legacy."]
            .iter()
            .any(|prefix| value.starts_with(prefix))
        && value.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
        && value.split(['.', '_', '-']).all(|segment| {
            !segment.is_empty()
                && segment
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        })
}

fn valid_route(route: &str) -> bool {
    route.len() <= 1024
        && (route == "/"
            || (route.starts_with('/')
                && route[1..].split('/').all(|segment| {
                    !segment.is_empty()
                        && segment
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                })))
}

#[cfg(test)]
pub(crate) fn test_binding() -> VerifiedFrontendEntry {
    resolve(&serde_json::json!({"frontend": {"default_entry_id": "main", "entries": [{
        "entry_id": "main", "route": "/workbench", "match": "exact",
        "contribution_id": "fixture.main", "implementation": "fixture.workbench", "label": "Workbench"
    }]}}), &format!("sha256:{}", "a".repeat(64)), None).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map() -> Value {
        json!({"frontend": {"default_entry_id": "work", "entries": [
            {"entry_id": "chat", "route": "/conversation", "match": "exact",
             "contribution_id": "example.chat", "implementation": "example.chat", "label": "Chat"},
            {"entry_id": "work", "route": "/workbench", "match": "exact",
             "contribution_id": "example.work", "implementation": "example.work", "label": "Workbench"}
        ]}})
    }

    #[test]
    fn non_chat_default_and_explicit_profile_entry_are_resolved_without_first_fallback() {
        assert_eq!(
            resolve(&map(), "digest", None).unwrap().entry.route,
            "/workbench"
        );
        assert_eq!(
            resolve(&map(), "digest", Some(&json!("chat")))
                .unwrap()
                .entry
                .route,
            "/conversation"
        );
        for selected in [json!("unknown"), json!(null), json!(["chat"])] {
            assert!(resolve(&map(), "digest", Some(&selected)).is_err());
        }
        let mut missing = map();
        missing["frontend"]["default_entry_id"] = json!("absent");
        assert!(resolve(&missing, "digest", None).is_err());
        assert!(resolve(&json!({}), "digest", None).is_err());
    }

    #[test]
    fn external_query_fragment_traversal_and_ambiguous_entries_are_rejected() {
        for route in [
            "https://example.test",
            "//host",
            "/chat?token=x",
            "/chat#code",
            "/../chat",
            "/%2fchat",
            "/a//b",
            "/a/",
        ] {
            let mut invalid = map();
            invalid["frontend"]["entries"][1]["route"] = json!(route);
            assert!(resolve(&invalid, "digest", None).is_err(), "{route}");
        }
        for field in ["entry_id", "route", "contribution_id"] {
            let mut invalid = map();
            invalid["frontend"]["entries"][1][field] =
                invalid["frontend"]["entries"][0][field].clone();
            assert!(resolve(&invalid, "digest", None).is_err(), "{field}");
        }
    }

    #[test]
    fn launch_binding_detects_map_route_and_implementation_changes() {
        let first = resolve(&map(), "map-a", None).unwrap();
        assert_ne!(first, resolve(&map(), "map-b", None).unwrap());
        for (field, value) in [
            ("route", "/different"),
            ("implementation", "example.other"),
            ("contribution_id", "example.other"),
        ] {
            let mut changed = map();
            changed["frontend"]["entries"][1][field] = json!(value);
            assert_ne!(first, resolve(&changed, "map-a", None).unwrap());
        }
    }
}
