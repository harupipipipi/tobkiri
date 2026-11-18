use std::num::NonZeroU32;

use super::{Error, ErrorCode, Settings, error};

const MODEL_ROUTING: &str = "local.model-registry.routing@1.0";
const PROVIDER_ROUTING: &str = "local.provider-registry.routing@1.0";

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ModelRoute {
    model_id: String,
    provider_instance_id: String,
    #[serde(deserialize_with = "serde::Deserialize::deserialize")]
    max_tokens: Option<NonZeroU32>,
    #[serde(deserialize_with = "serde::Deserialize::deserialize")]
    temperature: Option<String>,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderRoute {
    base_url: String,
}

pub(super) fn resolve(
    identifier: &str,
    timeout_ms: u32,
    mut call_port: impl FnMut(&str, &str, &str) -> Result<String, Error>,
) -> Result<(Settings, Option<NonZeroU32>), Error> {
    if identifier.trim().is_empty() {
        return Err(error(ErrorCode::Invalid, "select a registered model"));
    }
    let invalid_model = || error(ErrorCode::Invalid, "registered model route is invalid");
    let model = call_port(
        MODEL_ROUTING,
        "resolve-model",
        &serde_json::json!({"identifier": identifier}).to_string(),
    )?;
    let model: ModelRoute = serde_json::from_str(&model).map_err(|_| invalid_model())?;
    let temperature = match model.temperature {
        Some(value) => value.parse::<f64>().map_err(|_| invalid_model())?,
        None => 0.0,
    };
    if model.model_id.trim().is_empty()
        || model.provider_instance_id.trim().is_empty()
        || !(0.0..=2.0).contains(&temperature)
    {
        return Err(invalid_model());
    }

    let provider = call_port(
        PROVIDER_ROUTING,
        "resolve-local",
        &serde_json::json!({"provider": model.provider_instance_id}).to_string(),
    )?;
    let provider: ProviderRoute = serde_json::from_str(&provider)
        .map_err(|_| error(ErrorCode::Invalid, "registered provider route is invalid"))?;
    let mut settings = Settings {
        base_url: provider.base_url,
        model: model.model_id,
        secret_name: None,
        timeout_ms,
        temperature,
    };
    settings.validate()?;
    let url = url::Url::parse(&settings.base_url)
        .map_err(|_| error(ErrorCode::Invalid, "registered provider route is invalid"))?;
    let loopback = match url.host() {
        Some(url::Host::Ipv4(ip)) => ip == std::net::Ipv4Addr::LOCALHOST,
        Some(url::Host::Ipv6(ip)) => ip == std::net::Ipv6Addr::LOCALHOST,
        _ => false,
    };
    if url.scheme() != "http"
        || !loopback
        || url.port().is_none_or(|port| port < 1024)
        || url.path() != "/v1"
    {
        return Err(error(
            ErrorCode::Denied,
            "registered provider is outside the keyless local route",
        ));
    }
    Ok((settings, model.max_tokens))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Prompt, request_body};
    #[test]
    fn resolved_request_uses_saved_provider_and_exact_model_with_token_cap() {
        let mut calls = 0;
        let (settings, cap) = resolve("saved:chat-model", 3200, |shape, op, args| {
            calls += 1;
            match calls {
                1 => {
                    assert_eq!((shape, op), (MODEL_ROUTING, "resolve-model"));
                    assert_eq!(args, r#"{"identifier":"saved:chat-model"}"#);
                    Ok(r#"{"model_id":"org/Chat-Model","provider_instance_id":"local-1","max_tokens":1024,"temperature":"0.25"}"#.into())
                }
                2 => {
                    assert_eq!((shape, op), (PROVIDER_ROUTING, "resolve-local"));
                    assert_eq!(args, r#"{"provider":"local-1"}"#);
                    Ok(r#"{"base_url":"http://127.0.0.1:18080/"}"#.into())
                }
                _ => panic!("unexpected routing call"),
            }
        })
        .unwrap();
        assert_eq!(calls, 2);
        assert_eq!(settings.base_url, "http://127.0.0.1:18080/v1");
        assert!(settings.secret_name.is_none());
        assert_eq!(settings.timeout_ms, 3200);

        for (requested, expected) in [(0, 1), (16, 16), (2048, 1024)] {
            let prompt = Prompt {
                model: "saved:chat-model".into(),
                messages: vec![],
                max_tokens: requested,
            };
            let body: serde_json::Value =
                serde_json::from_slice(&request_body(&settings, prompt, cap)).unwrap();
            assert_eq!(body["model"], "org/Chat-Model");
            assert_eq!(body["temperature"], 0.25);
            assert_eq!(body["max_tokens"], expected);
            assert_eq!(body["stream"], true);
            assert_eq!(body["stream_options"]["include_usage"], true);
        }
    }

    #[test]
    fn missing_selection_and_malformed_model_routes_stop_before_provider_resolution() {
        assert!(resolve(" ", 1000, |_, _, _| panic!("unexpected routing call")).is_err());
        for reply in [
            r#"{"model_id":"id","provider_instance_id":"local-1","temperature":null}"#,
            r#"{"model_id":"id","provider_instance_id":"local-1","max_tokens":0,"temperature":null}"#,
            r#"{"model_id":"id","provider_instance_id":"local-1","max_tokens":null,"temperature":"NaN"}"#,
            r#"{"model_id":"id","provider_instance_id":" ","max_tokens":null,"temperature":null}"#,
        ] {
            let result = resolve("saved:model", 1000, |shape, _, _| {
                assert_eq!(shape, MODEL_ROUTING);
                Ok(reply.into())
            });
            assert!(matches!(result, Err(e) if matches!(e.code, ErrorCode::Invalid)));
        }
    }

    #[test]
    fn provider_resolution_errors_stop_the_request_and_nullable_fields_use_defaults() {
        let resolve_with_provider = |provider_reply: Result<String, Error>| {
            let mut provider_reply = Some(provider_reply);
            resolve("saved:model", 1000, |shape, _, _| {
                match shape {
                MODEL_ROUTING => Ok(r#"{"model_id":"id","provider_instance_id":"local-1","max_tokens":null,"temperature":null}"#.into()),
                PROVIDER_ROUTING => provider_reply.take().expect("provider called once"),
                _ => panic!("unexpected routing call"),
            }
            })
        };
        let denied = resolve_with_provider(Err(error(ErrorCode::Denied, "provider denied")));
        assert!(matches!(denied, Err(e) if matches!(e.code, ErrorCode::Denied)));
        for reply in [
            r#"{"base_url":"http://user:key@127.0.0.1:18080/v1"}"#,
            r#"{"base_url":"http://127.0.0.1:18080/v1","secret_name":"api-key"}"#,
        ] {
            let invalid = resolve_with_provider(Ok(reply.into()));
            assert!(matches!(invalid, Err(e) if matches!(e.code, ErrorCode::Invalid)));
        }
        for endpoint in [
            "https://provider.example/v1",
            "http://127.0.0.1:18080/other",
            "http://127.0.0.1:80/v1",
        ] {
            let denied =
                resolve_with_provider(Ok(serde_json::json!({"base_url":endpoint}).to_string()));
            assert!(matches!(denied, Err(e) if matches!(e.code, ErrorCode::Denied)));
        }

        let (settings, cap) =
            resolve_with_provider(Ok(r#"{"base_url":"http://127.0.0.1:18080/v1"}"#.into()))
                .unwrap();
        assert_eq!(settings.temperature, 0.0);
        assert!(cap.is_none());
    }
}
