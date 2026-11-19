//! Resolve the selected keyless route from the original Profile Registry bytes.

use crate::{record, registry::validate_document, state::get};
use ordered_python_compat::{PyValue, loads};
use std::{fmt, net::SocketAddr};

const MAX_DOCUMENT_BYTES: usize = 1024 * 1024;
const LOCAL_ADAPTER: &str = "local-openai-compatible";

/// The connection identity and endpoint observed by the Host before dispatch.
#[derive(Clone, Copy, Debug)]
pub struct LegacyLocalChatSelection<'a> {
    /// The exact instance ID from the Host-bound Registry read.
    pub connection_id: &'a str,
    /// The positive revision of the selected persisted record.
    pub record_revision: u64,
    /// The observed loopback OpenAI base URL, with optional trailing slash.
    pub endpoint: &'a str,
}

/// Terminal failures resolving a stored connection for native local chat.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyLocalChatSourceError {
    /// The supplied Registry cannot represent the original Profile source.
    InvalidDocument,
    /// The supplied Profile or connection observation is invalid.
    InvalidSelection,
    /// The stored connection no longer matches the exact Host observation.
    SelectionChanged,
}

impl fmt::Display for LegacyLocalChatSourceError {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        output.write_str(match self {
            Self::InvalidDocument => "local chat Registry document is invalid",
            Self::InvalidSelection => "local chat selection is invalid",
            Self::SelectionChanged => "local chat connection selection changed",
        })
    }
}

impl std::error::Error for LegacyLocalChatSourceError {}

/// Verify the complete original Registry and resolve its exact selected numeric loopback address.
pub fn resolve_legacy_local_chat_address(
    body: &[u8],
    profile_id: &str,
    selected: &LegacyLocalChatSelection<'_>,
) -> Result<SocketAddr, LegacyLocalChatSourceError> {
    use LegacyLocalChatSourceError::{InvalidDocument, InvalidSelection, SelectionChanged};

    if body.len() > MAX_DOCUMENT_BYTES {
        return Err(InvalidDocument);
    }
    if record::validate_profile_id(&PyValue::String(profile_id.into())).as_deref() != Ok(profile_id)
        || selected.connection_id.len() > 256
        || record::identifier(&PyValue::String(selected.connection_id.into())).as_deref()
            != Ok(selected.connection_id)
        || selected.record_revision == 0
        || selected.endpoint.len() > 2048
    {
        return Err(InvalidSelection);
    }

    let local_adapter = PyValue::String(LOCAL_ADAPTER.into());
    let observed_endpoint = record::provider_endpoint(
        &PyValue::String(selected.endpoint.into()),
        &PyValue::Null,
        &local_adapter,
    )
    .map_err(|_| InvalidSelection)?;
    let document = loads(std::str::from_utf8(body).map_err(|_| InvalidDocument)?)
        .map_err(|_| InvalidDocument)?;
    let providers = validate_document(&document, profile_id).map_err(|_| InvalidDocument)?;
    if document.get("revision").is_none() {
        return Err(InvalidDocument);
    }

    let mut matches = providers.iter().filter(|(_, record)| {
        get(record, "provider_instance_id").as_str() == Some(selected.connection_id)
    });
    let (key, record) = matches.next().ok_or(SelectionChanged)?;
    if key != selected.connection_id
        || matches.next().is_some()
        || get(record, "adapter_id") != &local_adapter
        || get(record, "enabled") != &PyValue::Bool(true)
        || get(record, "credential_handle") != &PyValue::Null
        || !matches!(get(record, "record_revision"), PyValue::Int(revision)
            if revision.parse::<u64>() == Ok(selected.record_revision))
    {
        return Err(SelectionChanged);
    }
    let endpoint =
        record::provider_endpoint(get(record, "endpoint"), &PyValue::Null, &local_adapter)
            .map_err(|_| InvalidDocument)?;
    if endpoint != observed_endpoint {
        return Err(SelectionChanged);
    }

    // The canonical endpoint policy already proved the host is numeric loopback.
    endpoint
        .as_str()
        .and_then(|value| value.strip_prefix("http://")?.strip_suffix("/v1"))
        .and_then(|authority| authority.parse().ok())
        .ok_or(InvalidDocument)
}

#[cfg(test)]
mod tests {
    use super::*;
    use LegacyLocalChatSourceError::{InvalidDocument, InvalidSelection, SelectionChanged};
    use serde_json::{Value, json};

    const FIXTURE: &[u8] = include_bytes!(concat!(
        "../evidence/original-python/cases/local-endpoint-canonicalization/actions/",
        "save-revision-0.store-after.json"
    ));
    const SELECTED: LegacyLocalChatSelection<'static> = LegacyLocalChatSelection {
        connection_id: "local/model",
        record_revision: 1,
        endpoint: "http://[::1]:1234/v1",
    };

    fn fixture() -> Value {
        serde_json::from_slice(FIXTURE).unwrap()
    }

    fn resolve(
        document: &Value,
        selected: &LegacyLocalChatSelection<'_>,
    ) -> Result<SocketAddr, LegacyLocalChatSourceError> {
        resolve_legacy_local_chat_address(
            &serde_json::to_vec(document).unwrap(),
            "defaults",
            selected,
        )
    }

    #[test]
    fn resolves_an_original_python_save_and_equivalent_canonical_local_addresses() {
        assert_eq!(
            resolve_legacy_local_chat_address(FIXTURE, "defaults", &SELECTED),
            Ok("[::1]:1234".parse().unwrap()),
        );

        let mut document = fixture();
        for (authority, saved_suffix, observed_suffix) in
            [("127.0.0.1:1024", "/", ""), ("[::1]:65535", "", "/")]
        {
            document["providers"][SELECTED.connection_id]["endpoint"] =
                json!(format!("http://{authority}/v1{saved_suffix}"));
            let endpoint = format!("http://{authority}/v1{observed_suffix}");
            let selected = LegacyLocalChatSelection {
                endpoint: &endpoint,
                ..SELECTED
            };
            assert_eq!(
                resolve(&document, &selected),
                Ok(authority.parse().unwrap())
            );
        }
    }

    #[test]
    fn rejects_conflicting_connection_identity_revision_or_configuration() {
        for (field, value, error) in [
            ("provider_instance_id", json!("other"), SelectionChanged),
            ("record_revision", json!(2), SelectionChanged),
            ("record_revision", json!("1"), SelectionChanged),
            ("record_revision", json!(true), SelectionChanged),
            ("enabled", json!(false), SelectionChanged),
            ("enabled", json!(1), SelectionChanged),
            ("adapter_id", json!("openai-compatible"), SelectionChanged),
            ("endpoint", json!("http://[::1]:4321/v1"), SelectionChanged),
            (
                "endpoint",
                json!("http://localhost:1234/v1"),
                InvalidDocument,
            ),
            (
                "credential_handle",
                json!("opaque:fixture"),
                InvalidDocument,
            ),
        ] {
            let mut document = fixture();
            document["providers"][SELECTED.connection_id][field] = value;
            assert_eq!(resolve(&document, &SELECTED), Err(error), "{field}");
        }
        let mut document = fixture();
        let providers = document["providers"].as_object_mut().unwrap();
        let record = providers.get(SELECTED.connection_id).unwrap().clone();
        providers.insert("another-key".into(), record);
        assert_eq!(resolve(&document, &SELECTED), Err(SelectionChanged));
        document["providers"]
            .as_object_mut()
            .unwrap()
            .remove(SELECTED.connection_id);
        assert_eq!(resolve(&document, &SELECTED), Err(SelectionChanged));
    }

    #[test]
    fn checks_the_whole_profile_before_using_a_matching_connection() {
        assert_eq!(
            resolve_legacy_local_chat_address(FIXTURE, "other", &SELECTED),
            Err(InvalidDocument),
        );
        for (key, value) in [
            ("version", json!("unrecognized")),
            ("providers", json!([])),
            ("profile_id", json!(null)),
        ] {
            let mut document = fixture();
            document[key] = value;
            assert_eq!(resolve(&document, &SELECTED), Err(InvalidDocument));
        }
        for unrelated in [
            json!([]),
            json!({"model_access": {}}),
            json!({"endpoint": "invalid"}),
        ] {
            let mut document = fixture();
            document["providers"]["unrelated"] = unrelated;
            assert_eq!(resolve(&document, &SELECTED), Err(InvalidDocument));
        }
        let mut document = fixture();
        document.as_object_mut().unwrap().remove("revision");
        assert_eq!(resolve(&document, &SELECTED), Err(InvalidDocument));
    }

    #[test]
    fn rejects_unbound_selections_without_interpreting_a_destination() {
        for endpoint in [
            "http://127.0.0.1:1023/v1",
            "http://[::1]:65536/v1",
            "http://127.0.0.2:1234/v1",
            "http://localhost:1234/v1",
            "http://[::1]:1234/v1?",
            "http://[::1]:1234/v1//",
        ] {
            assert_eq!(
                resolve(
                    &fixture(),
                    &LegacyLocalChatSelection {
                        endpoint,
                        ..SELECTED
                    }
                ),
                Err(InvalidSelection),
            );
        }
        for selected in [
            LegacyLocalChatSelection {
                connection_id: " local/model",
                ..SELECTED
            },
            LegacyLocalChatSelection {
                record_revision: 0,
                ..SELECTED
            },
        ] {
            assert_eq!(resolve(&fixture(), &selected), Err(InvalidSelection));
        }
    }

    #[test]
    fn bounds_the_document_and_rejects_ambiguous_or_unreadable_bytes() {
        let mut exact = FIXTURE.to_vec();
        exact.resize(MAX_DOCUMENT_BYTES, b' ');
        assert_eq!(
            resolve_legacy_local_chat_address(&exact, "defaults", &SELECTED),
            Ok("[::1]:1234".parse().unwrap()),
        );
        exact.push(b' ');
        let duplicate = std::str::from_utf8(FIXTURE).unwrap().replace(
            "\"local/model\": {",
            "\"local/model\": {}, \"local\\u002fmodel\": {",
        );
        let dense = std::str::from_utf8(FIXTURE).unwrap().replace(
            "\"migration\": null",
            &format!(
                "\"migration\": [{}null]",
                "null,".repeat(ordered_python_compat::MAX_NODES),
            ),
        );
        for bytes in [
            exact.as_slice(),
            duplicate.as_bytes(),
            dense.as_bytes(),
            b"{\xff}",
            b"{",
        ] {
            assert_eq!(
                resolve_legacy_local_chat_address(bytes, "defaults", &SELECTED),
                Err(InvalidDocument),
            );
        }
    }
}
