//! Bind a requested model to its current public catalog slug before inspection.
//! A catalog observation never authorizes a POST or proves historical aliases.

use super::{Endpoint, HttpPayload, Observation, Selection, lookup_with};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const BASE: &str = "https://openrouter.ai/api/v1/model/";

#[derive(Deserialize)]
struct CatalogEnvelope {
    data: CatalogModel,
}

#[derive(Deserialize)]
struct CatalogModel {
    id: String,
    canonical_slug: String,
}

#[derive(Serialize)]
pub struct ModelMapping {
    version: &'static str,
    requested_model: String,
    canonical_model: String,
    source_url: String,
    response_digest: String,
}

#[derive(Serialize)]
pub struct RequestedObservation {
    version: &'static str,
    requested_model: String,
    model_mapping: ModelMapping,
    observation: Observation,
}

#[derive(Debug, PartialEq, Eq)]
pub enum CatalogError<E> {
    InvalidModel,
    Unavailable,
    InvalidResponse,
    Core(E),
}

fn valid_model(value: &str) -> bool {
    if value.len() > 256 {
        return false;
    }
    let mut parts = value.split('/');
    let valid_part = |part: &str| {
        !part.is_empty()
            && part != "."
            && part != ".."
            && part
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b))
    };
    parts.next().is_some_and(valid_part)
        && parts.next().is_some_and(valid_part)
        && parts.next().is_none()
}

fn source_url(model: &str) -> Option<String> {
    if !valid_model(model) {
        return None;
    }
    // The slash separates exactly two validated path segments. A colon is
    // encoded; query/fragment, percent, dot segments and extra slash are refused.
    Some(format!("{BASE}{}", model.replace(':', "%3A")))
}

pub fn lookup_requested_with<E>(
    original: &Selection,
    mut fetch: impl FnMut(Endpoint, &str) -> Result<HttpPayload, E>,
) -> Result<RequestedObservation, CatalogError<E>> {
    let url = source_url(&original.expected_model).ok_or(CatalogError::InvalidModel)?;
    let payload = fetch(Endpoint::ModelCatalog, &url).map_err(CatalogError::Core)?;
    if payload.status != 200 {
        return Err(CatalogError::Unavailable);
    }
    if payload.oversized || payload.body.len() > Endpoint::ModelCatalog.maximum_bytes() {
        return Err(CatalogError::InvalidResponse);
    }
    let catalog: CatalogEnvelope =
        serde_json::from_slice(&payload.body).map_err(|_| CatalogError::InvalidResponse)?;
    if catalog.data.id != original.expected_model || !valid_model(&catalog.data.canonical_slug) {
        return Err(CatalogError::InvalidResponse);
    }
    let selection = Selection {
        generation_id: original.generation_id.clone(),
        expected_model: catalog.data.canonical_slug.clone(),
        expected_provider: original.expected_provider.clone(),
        include_public_content: original.include_public_content,
    };
    let observation = lookup_with(&selection, &mut fetch).map_err(CatalogError::Core)?;
    Ok(RequestedObservation {
        version: "tobkiri.requested-generation-observation.v1",
        requested_model: original.expected_model.clone(),
        model_mapping: ModelMapping {
            version: "openrouter.model-mapping.v1",
            requested_model: original.expected_model.clone(),
            canonical_model: catalog.data.canonical_slug,
            source_url: url,
            response_digest: format!("sha256:{:x}", Sha256::digest(&payload.body)),
        },
        observation,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn selection(content: bool) -> Selection {
        Selection::new(
            "gen-public-123".into(),
            "vendor/model".into(),
            Some("fixture-provider".into()),
            content,
        )
        .unwrap()
    }

    fn payload(value: Value) -> HttpPayload {
        HttpPayload {
            status: 200,
            body: value.to_string().into_bytes(),
            oversized: false,
        }
    }

    #[test]
    fn fixed_catalog_pair_precedes_metadata_and_keeps_original_model() {
        let mut calls = vec![];
        let output = lookup_requested_with(&selection(true), |endpoint, url| {
            calls.push((endpoint, url.to_owned()));
            Ok::<_, ()>(match endpoint {
                Endpoint::ModelCatalog => payload(json!({"data":{
                    "id":"vendor/model", "canonical_slug":"vendor/model-20261017",
                    "unrelated_private_field":"not exported"}})),
                Endpoint::Metadata => payload(json!({"data":{
                    "id":"gen-public-123", "model":"vendor/model-20261017",
                    "provider_name":"fixture-provider", "finish_reason":"stop",
                    "cancelled":false,"native_tokens_prompt":3,"native_tokens_completion":2,
                    "total_cost":0.00000123}})),
                Endpoint::PublicContent => HttpPayload {
                    status: 404,
                    body: vec![],
                    oversized: false,
                },
            })
        })
        .unwrap();
        assert_eq!(
            calls.iter().map(|x| x.0).collect::<Vec<_>>(),
            [
                Endpoint::ModelCatalog,
                Endpoint::Metadata,
                Endpoint::PublicContent
            ]
        );
        assert_eq!(
            calls[0].1,
            "https://openrouter.ai/api/v1/model/vendor/model"
        );
        let out = serde_json::to_value(output).unwrap();
        assert_eq!(out["requested_model"], "vendor/model");
        assert_eq!(
            out["observation"]["metadata"]["model"],
            "vendor/model-20261017"
        );
        assert_eq!(out["observation"]["content"]["availability"], "not_found");
        assert!(
            out["model_mapping"]["response_digest"]
                .as_str()
                .unwrap()
                .starts_with("sha256:")
        );
        assert!(!out.to_string().contains("unrelated_private_field"));
        assert!(!out.to_string().contains("not exported"));
    }

    #[test]
    fn absent_wrong_malformed_oversized_or_http_failed_catalog_stops_before_generation() {
        for response in [
            HttpPayload {
                status: 404,
                body: vec![],
                oversized: false,
            },
            payload(json!({"data":{"id":"wrong/model","canonical_slug":"vendor/model-v1"}})),
            payload(json!({"data":{"id":"vendor/model","canonical_slug":"../escape"}})),
            payload(json!({"data":{"id":"vendor/model"}})),
            HttpPayload {
                status: 200,
                body: vec![],
                oversized: true,
            },
        ] {
            let mut remaining = Some(response);
            let mut count = 0;
            assert!(
                lookup_requested_with(&selection(true), |endpoint, _| {
                    assert_eq!(endpoint, Endpoint::ModelCatalog);
                    count += 1;
                    Ok::<_, ()>(remaining.take().unwrap())
                })
                .is_err()
            );
            assert_eq!(count, 1);
        }
    }

    #[test]
    fn model_path_rejects_ambiguous_or_injectable_routes() {
        for model in [
            "../m",
            "x/..",
            "x/y/z",
            "https://bad",
            "x/y?z",
            "x/y#z",
            "x/%2f",
            "x/猫",
            "/x",
            "x/",
            "x/y\\z",
            "x/\ny",
        ] {
            assert_eq!(source_url(model), None, "{model}");
        }
        assert_eq!(
            source_url("x/y:free").unwrap(),
            "https://openrouter.ai/api/v1/model/x/y%3Afree"
        );
    }
}
