use crate::{
    failure::{self, SourceFailure, SourceResult},
    model_access::identifier,
    obj, python_url,
};
use ordered_python_compat::{PyValue, pystr};

pub const REVISION: &str = "openrouter.official-filters.2026-10-07.v1";
const DISCOVERY: &[&str] = &["output_modalities", "supported_parameters", "category"];
const ROUTING: &[&str] = &[
    "only",
    "ignore",
    "order",
    "quantizations",
    "require_parameters",
    "data_collection",
    "zdr",
    "max_price",
    "sort",
];
const MODALITIES: &[&str] = &[
    "text",
    "image",
    "embeddings",
    "audio",
    "video",
    "rerank",
    "decisions",
    "speech",
    "transcription",
    "all",
];
const CATEGORIES: &[&str] = &[
    "programming",
    "roleplay",
    "marketing",
    "marketing/seo",
    "technology",
    "science",
    "translation",
    "legal",
    "finance",
    "health",
    "trivia",
    "academia",
];
const QUANTIZATIONS: &[&str] = &[
    "int4", "int8", "fp4", "mxfp4", "nvfp4", "fp6", "fp8", "mxfp8", "fp16", "bf16", "fp32",
    "unknown",
];
const MAX_MICRO_USD: u64 = (1u64 << 53) - 1;
const PRICE_ERROR: &str = "native price must be bounded nonnegative plain-decimal USD";

pub fn official(connection: &PyValue) -> SourceResult<bool> {
    let metadata = failure::get(connection, "metadata")?;
    let empty = PyValue::Object(Vec::new());
    let metadata = if metadata.as_object().is_some() {
        metadata
    } else {
        &empty
    };
    let endpoint = failure::get(connection, "endpoint")?;
    let endpoint = if ordered_python_compat::pytruthy(endpoint) {
        pystr(endpoint)
    } else {
        String::new()
    };
    // urlsplit eagerly checks bracket/NFKC syntax, but .port is lazy. Preserve
    // that distinction: a foreign adapter/catalog/host need not read .port.
    let endpoint = python_url::split(&endpoint)?;
    let adapter = failure::get(connection, "adapter_id")?;
    failure::hashable(adapter)?;
    if !adapter
        .as_str()
        .is_some_and(|adapter| ["openai", "openai-compatible"].contains(&adapter))
        || metadata
            .get("catalog_provider_id")
            .and_then(PyValue::as_str)
            != Some("openrouter")
        || endpoint.scheme != "https"
        || endpoint.hostname.as_deref() != Some("openrouter.ai")
    {
        return Ok(false);
    }
    Ok(endpoint.port()?.is_none_or(|port| port == 443)
        && !endpoint.userinfo
        && endpoint.path.trim_end_matches('/') == "/api/v1"
        && endpoint.query.is_empty()
        && endpoint.fragment.is_empty())
}

fn tokens(value: &PyValue, allowed: Option<&[&str]>) -> SourceResult<PyValue> {
    let Some(values) = value
        .as_array()
        .filter(|values| !values.is_empty() && values.len() <= 64)
    else {
        return Err(SourceFailure::value("native filter list is invalid"));
    };
    let mut result: Vec<PyValue> = Vec::new();
    for value in values {
        let Some(token) = value.as_str().filter(|token| {
            identifier(token, 128, false) && allowed.is_none_or(|allowed| allowed.contains(token))
        }) else {
            return Err(SourceFailure::value("native filter value is invalid"));
        };
        if !result.iter().any(|value| value.as_str() == Some(token)) {
            result.push(PyValue::String(token.into()));
        }
    }
    Ok(PyValue::Array(result))
}

pub fn normalize(value: &PyValue, verified: bool) -> SourceResult<PyValue> {
    if matches!(value, PyValue::Null) {
        return Ok(PyValue::Object(Vec::new()));
    }
    if !verified {
        return Err(SourceFailure::value(
            "native filters are unverified for this connection",
        ));
    }
    let Some(entries) = value.as_object() else {
        return Err(SourceFailure::value(
            "native filter capability revision is unknown",
        ));
    };
    if entries
        .iter()
        .any(|(key, _)| !["revision", "discovery", "routing"].contains(&key.as_str()))
        || value.get("revision").and_then(PyValue::as_str) != Some(REVISION)
    {
        return Err(SourceFailure::value(
            "native filter capability revision is unknown",
        ));
    }
    let empty = PyValue::Object(Vec::new());
    let discovery = value
        .get("discovery")
        .unwrap_or(&empty)
        .as_object()
        .filter(|entries| {
            entries
                .iter()
                .all(|(key, _)| DISCOVERY.contains(&key.as_str()))
        })
        .ok_or_else(|| SourceFailure::value("native discovery filter is unknown"))?;
    let routing = value
        .get("routing")
        .unwrap_or(&empty)
        .as_object()
        .filter(|entries| {
            entries
                .iter()
                .all(|(key, _)| ROUTING.contains(&key.as_str()))
        })
        .ok_or_else(|| SourceFailure::value("native routing filter is unknown"))?;
    let mut normalized_discovery = Vec::new();
    for (key, value) in discovery {
        let value = if key == "category" {
            if value
                .as_str()
                .is_none_or(|value| !CATEGORIES.contains(&value))
            {
                return Err(SourceFailure::value("native category is invalid"));
            }
            value.clone()
        } else {
            let values = tokens(
                value,
                if key == "output_modalities" {
                    Some(MODALITIES)
                } else {
                    None
                },
            )?;
            let items = values.as_array().unwrap();
            if items.iter().any(|value| value.as_str() == Some("all")) && items.len() != 1 {
                return Err(SourceFailure::value("all modalities cannot be combined"));
            }
            values
        };
        normalized_discovery.push((key.clone(), value));
    }
    let mut normalized_routing = Vec::new();
    for (key, value) in routing {
        let value = match key.as_str() {
            "only" | "ignore" | "order" | "quantizations" => tokens(
                value,
                if key == "quantizations" {
                    Some(QUANTIZATIONS)
                } else {
                    None
                },
            )?,
            "require_parameters" | "zdr" => {
                if !matches!(value, PyValue::Bool(_)) {
                    return Err(SourceFailure::value("native boolean is invalid"));
                }
                value.clone()
            }
            "data_collection" | "sort" => {
                let choices: &[&str] = if key == "data_collection" {
                    &["allow", "deny"]
                } else {
                    &["price", "latency", "throughput"]
                };
                if value.as_str().is_none_or(|value| !choices.contains(&value)) {
                    return Err(SourceFailure::value("native enum is invalid"));
                }
                value.clone()
            }
            "max_price" => {
                let entries = value
                    .as_object()
                    .filter(|entries| {
                        !entries.is_empty()
                            && entries.iter().all(|(key, _)| {
                                ["prompt", "completion", "request", "image"].contains(&key.as_str())
                            })
                    })
                    .ok_or_else(|| SourceFailure::value("native price fields are invalid"))?;
                PyValue::Object(
                    entries
                        .iter()
                        .map(|(key, value)| Ok((key.clone(), normalize_price(value)?)))
                        .collect::<SourceResult<_>>()?,
                )
            }
            _ => unreachable!("routing key checked above"),
        };
        normalized_routing.push((key.clone(), value));
    }
    let routing = PyValue::Object(normalized_routing);
    if let (Some(only), Some(ignore)) = (
        routing.get("only").and_then(PyValue::as_array),
        routing.get("ignore").and_then(PyValue::as_array),
    ) && only.iter().any(|value| ignore.contains(value))
    {
        return Err(SourceFailure::value(
            "a provider cannot be allowed and excluded",
        ));
    }
    Ok(obj([
        ("revision", PyValue::String(REVISION.into())),
        ("discovery", PyValue::Object(normalized_discovery)),
        ("routing", routing),
    ]))
}

fn normalize_price(value: &PyValue) -> SourceResult<PyValue> {
    if let PyValue::Int(digits) = value {
        if digits
            .parse::<u64>()
            .is_ok_and(|value| value <= MAX_MICRO_USD / 1_000_000)
        {
            return Ok(value.clone());
        }
        return Err(SourceFailure::value(PRICE_ERROR));
    }
    let text = value
        .as_str()
        .ok_or_else(|| SourceFailure::value(PRICE_ERROR))?;
    let (whole, fraction) = text
        .split_once('.')
        .map_or((text, None), |(whole, fraction)| (whole, Some(fraction)));
    if whole.is_empty()
        || whole.len() > 10
        || (whole.len() > 1 && whole.starts_with('0'))
        || !whole.bytes().all(|byte| byte.is_ascii_digit())
        || fraction.is_some_and(|fraction| {
            fraction.is_empty()
                || fraction.len() > 6
                || !fraction.bytes().all(|byte| byte.is_ascii_digit())
        })
    {
        return Err(SourceFailure::value(PRICE_ERROR));
    }
    let fraction = fraction.unwrap_or("");
    let micro = whole.parse::<u64>().unwrap() * 1_000_000
        + if fraction.is_empty() {
            0
        } else {
            fraction.parse::<u64>().unwrap() * 10u64.pow((6 - fraction.len()) as u32)
        };
    if micro > MAX_MICRO_USD {
        return Err(SourceFailure::value(PRICE_ERROR));
    }
    let fraction = fraction.trim_end_matches('0');
    Ok(PyValue::String(if fraction.is_empty() {
        whole.into()
    } else {
        format!("{whole}.{fraction}")
    }))
}

// Exact decimal-to-micro comparison for Decimal(str(binary64)) == Decimal(text).
// The binary64 value is never used to normalize or persist a policy price.
fn decimal_micro(text: &str) -> Option<u64> {
    let (mantissa, exponent) = text
        .split_once(['e', 'E'])
        .map_or(Some((text, 0i32)), |(mantissa, exponent)| {
            Some((mantissa, exponent.parse::<i32>().ok()?))
        })?;
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let digits = format!("{whole}{fraction}").parse::<u64>().ok()?;
    let scale = 6i32
        .checked_add(exponent)?
        .checked_sub(i32::try_from(fraction.len()).ok()?)?;
    if scale >= 0 {
        digits.checked_mul(10u64.checked_pow(scale as u32)?)
    } else {
        let divisor = 10u64.checked_pow(scale.unsigned_abs())?;
        (digits % divisor == 0).then_some(digits / divisor)
    }
}

fn wire_price(value: &PyValue) -> SourceResult<PyValue> {
    if let PyValue::String(text) = value {
        if text.contains('.') {
            let numeric = text.parse::<f64>().map_err(|_| {
                SourceFailure::value("native price cannot be represented exactly for transport")
            })?;
            let shortest = pystr(&PyValue::Float(numeric));
            if decimal_micro(&shortest) != decimal_micro(text) {
                return Err(SourceFailure::value(
                    "native price cannot be represented exactly for transport",
                ));
            }
            return Ok(PyValue::Float(numeric));
        }
        return Ok(PyValue::Int(text.clone()));
    }
    Ok(value.clone())
}

pub fn apply(value: &PyValue, parameters: &PyValue) -> SourceResult<PyValue> {
    let normalized = normalize(value, true)?;
    if failure::contains(parameters, "provider")? {
        return Err(SourceFailure::value(
            "saved native routing cannot be overridden",
        ));
    }
    // In the source this is dict(parameters), not an isinstance(Mapping) guard.
    let (mut parameters, unrepresented_key) = crate::python_dict(parameters)?;
    let routing = normalized.get("routing").ok_or_else(|| SourceFailure {
        exception_class: "KeyError",
        message: "'routing'".into(),
    })?;
    if ordered_python_compat::pytruthy(routing) {
        let mut routing = routing.as_object().unwrap().to_vec();
        if let Some((_, prices)) = routing.iter_mut().find(|(key, _)| key == "max_price") {
            *prices = PyValue::Object(
                prices
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(key, value)| Ok((key.clone(), wire_price(value)?)))
                    .collect::<SourceResult<_>>()?,
            );
        }
        routing.push(("allow_fallbacks".into(), PyValue::Bool(false)));
        if let Some((_, value)) = parameters.iter_mut().find(|(key, _)| key == "provider") {
            *value = PyValue::Object(routing);
        } else {
            parameters.push(("provider".into(), PyValue::Object(routing)));
        }
    }
    if unrepresented_key {
        return Err(SourceFailure {
            exception_class: "ObservationBoundary",
            message:
                "non-string Python dictionary keys are outside this observation representation"
                    .into(),
        });
    }
    Ok(PyValue::Object(parameters))
}

pub fn discovery_url(value: &PyValue) -> SourceResult<PyValue> {
    let normalized = normalize(value, true)?;
    let mut query = vec![("output_modalities".to_owned(), "all".to_owned())];
    // normalize(None) returns {}, so the original subsequent subscript fails.
    let discovery = normalized.get("discovery").ok_or_else(|| SourceFailure {
        exception_class: "KeyError",
        message: "'discovery'".into(),
    })?;
    for (key, value) in discovery.as_object().unwrap() {
        let value = match value {
            PyValue::Array(values) => values
                .iter()
                .map(|value| value.as_str().unwrap())
                .collect::<Vec<_>>()
                .join(","),
            _ => value.as_str().unwrap().to_owned(),
        };
        if let Some((_, old)) = query.iter_mut().find(|(name, _)| name == key) {
            *old = value;
        } else {
            query.push((key.clone(), value));
        }
    }
    Ok(PyValue::String(format!(
        "https://openrouter.ai/api/v1/models?{}",
        query
            .into_iter()
            .map(|(key, value)| format!("{}={}", quote_plus(&key), quote_plus(&value)))
            .collect::<Vec<_>>()
            .join("&")
    )))
}

fn quote_plus(value: &str) -> String {
    let mut result = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                result.push(char::from(byte))
            }
            b' ' => result.push('+'),
            _ => {
                use std::fmt::Write;
                write!(result, "%{byte:02X}").unwrap();
            }
        }
    }
    result
}
