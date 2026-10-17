//! Read-only OpenRouter generation observations through ordinary Core imports.
//! No model POST, checkpoint mutation, credential reveal or recovery transition.

wit_bindgen::generate!({ path: "wit", world: "pack", generate_all });

mod reader;

use exports::local::openrouter_generation_reader::generation_reader::{Guest, Selection};
use exports::tobkiri::core::lifecycle;
use std::cell::RefCell;
use tobkiri::core::{
    flow, network, secrets,
    types::{Error, ErrorCode},
};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    timeout_ms: u32,
}

thread_local! {
    static SETTINGS: RefCell<Option<Settings>> = const { RefCell::new(None) };
}

struct Pack;

fn error(code: ErrorCode, message: &'static str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}

fn safe_import_error(source: Error) -> Error {
    // Host errors retain their classification, but no provider error text,
    // request headers, input or response body is forwarded by this Pack.
    error(source.code, "Generation lookup Core operation failed")
}

impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let settings: Settings = serde_json::from_str(&config).map_err(|_| {
            error(
                ErrorCode::Invalid,
                "Invalid generation reader configuration",
            )
        })?;
        if settings.timeout_ms == 0 || settings.timeout_ms > 300_000 {
            return Err(error(
                ErrorCode::Invalid,
                "Invalid generation reader timeout",
            ));
        }
        SETTINGS.with_borrow_mut(|value| *value = Some(settings));
        Ok(())
    }

    fn shutdown() {
        SETTINGS.with_borrow_mut(|value| *value = None);
    }
}

fn fetch(
    endpoint: reader::Endpoint,
    url: &str,
    timeout_ms: u32,
) -> Result<reader::HttpPayload, Error> {
    if flow::is_cancelled() {
        return Err(error(ErrorCode::Cancelled, "Generation lookup cancelled"));
    }
    // get returns an opaque ref bound to this Pack's own principal. The Core
    // independently authorizes the GET and insertion into its header.
    let secret = if endpoint == reader::Endpoint::ModelCatalog {
        None
    } else {
        Some(secrets::get("api-key").map_err(safe_import_error)?)
    };
    let secret_headers = if secret.is_some() {
        vec![network::HeaderFromSecret {
            name: "authorization".into(),
            prefix: "Bearer ".into(),
            index: 0,
        }]
    } else {
        vec![]
    };
    let secret_refs = secret.iter().collect::<Vec<_>>();
    let response = network::fetch(
        &network::Request {
            method: "GET".into(),
            url: url.into(),
            headers: vec![("accept".into(), "application/json".into())],
            secret_headers,
            body: None,
            timeout_ms: Some(timeout_ms),
        },
        &secret_refs,
    )
    .map_err(safe_import_error)?;
    let status = response.status();
    // Do not read HTTP error bodies at all.
    if status != 200 {
        return Ok(reader::HttpPayload {
            status,
            body: Vec::new(),
            oversized: false,
        });
    }
    let maximum = endpoint.maximum_bytes();
    let mut body = Vec::new();
    loop {
        if flow::is_cancelled() {
            return Err(error(ErrorCode::Cancelled, "Generation lookup cancelled"));
        }
        let requested = (maximum - body.len() + 1).min(8192) as u32;
        let Some(bytes) = response.read(requested).map_err(safe_import_error)? else {
            break;
        };
        if bytes.is_empty() {
            // A zero-progress source is not EOF and must not spin forever.
            return Err(error(
                ErrorCode::Unavailable,
                "Generation lookup made no progress",
            ));
        }
        if bytes.len() > requested as usize || bytes.len() > maximum - body.len() {
            return Ok(reader::HttpPayload {
                status,
                body: Vec::new(),
                oversized: true,
            });
        }
        body.extend_from_slice(&bytes);
    }
    Ok(reader::HttpPayload {
        status,
        body,
        oversized: false,
    })
}

impl Guest for Pack {
    fn lookup_requested(request: Selection) -> Result<String, Error> {
        let selection = reader::Selection::new(
            request.generation_id,
            request.expected_model,
            request.expected_provider,
            request.include_public_content,
        )
        .map_err(|_| error(ErrorCode::Invalid, "Invalid generation lookup selection"))?;
        let timeout = SETTINGS
            .with_borrow(|settings| settings.as_ref().map(|s| s.timeout_ms))
            .ok_or_else(|| error(ErrorCode::Invalid, "Generation reader is not initialized"))?;
        let observation = reader::catalog::lookup_requested_with(&selection, |endpoint, url| {
            fetch(endpoint, url, timeout)
        })
        .map_err(|failure| match failure {
            reader::catalog::CatalogError::Core(source) => source,
            reader::catalog::CatalogError::InvalidModel => {
                error(ErrorCode::Invalid, "Invalid requested model catalog path")
            }
            reader::catalog::CatalogError::Unavailable => error(
                ErrorCode::Unavailable,
                "Requested model catalog is unavailable",
            ),
            reader::catalog::CatalogError::InvalidResponse => error(
                ErrorCode::Invalid,
                "Requested model catalog has no verified exact mapping",
            ),
        })?;
        let output = serde_json::to_string(&observation).map_err(|_| {
            error(
                ErrorCode::Internal,
                "Requested generation observation cannot be encoded",
            )
        })?;
        if output.len() > reader::MAX_OUTPUT_BYTES {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Requested generation observation exceeds its output bound",
            ));
        }
        Ok(output)
    }

    fn lookup(request: Selection) -> Result<String, Error> {
        let selection = reader::Selection::new(
            request.generation_id,
            request.expected_model,
            request.expected_provider,
            request.include_public_content,
        )
        .map_err(|_| error(ErrorCode::Invalid, "Invalid generation lookup selection"))?;
        let timeout = SETTINGS
            .with_borrow(|settings| settings.as_ref().map(|s| s.timeout_ms))
            .ok_or_else(|| error(ErrorCode::Invalid, "Generation reader is not initialized"))?;
        let observation =
            reader::lookup_with(&selection, |endpoint, url| fetch(endpoint, url, timeout))?;
        let output = serde_json::to_string(&observation).map_err(|_| {
            error(
                ErrorCode::Internal,
                "Generation observation cannot be encoded",
            )
        })?;
        if output.len() > reader::MAX_OUTPUT_BYTES {
            return Err(error(
                ErrorCode::LimitExceeded,
                "Generation observation exceeds its output bound",
            ));
        }
        Ok(output)
    }
}

export!(Pack);
