//! Only urllib.parse.urlsplit fields read by is_official_openrouter.
//! This is not a URL authority/canonicalization API. Percent escapes are not
//! decoded; port validation remains lazy, as in CPython 3.13 urllib.parse.
use crate::failure::{SourceFailure, SourceResult};
use ordered_python_compat::{PyValue, pyrepr};

pub struct Parts {
    pub scheme: String,
    pub hostname: Option<String>,
    pub userinfo: bool,
    pub path: String,
    pub query: String,
    pub fragment: String,
    port: Option<String>,
}
impl Parts {
    pub fn port(&self) -> SourceResult<Option<u16>> {
        let Some(port) = &self.port else {
            return Ok(None);
        };
        if !port.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(SourceFailure::value(format!(
                "Port could not be cast to integer value as {}",
                pyrepr(&PyValue::String(port.clone()))
            )));
        }
        if port.len() > ordered_python_compat::MAX_INTEGER_DIGITS {
            return Err(SourceFailure::value(format!(
                "Exceeds the limit ({} digits) for integer string conversion: value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
                ordered_python_compat::MAX_INTEGER_DIGITS,
                port.len()
            )));
        }
        let value = port.bytes().try_fold(0u32, |value, byte| {
            value.checked_mul(10)?.checked_add(u32::from(byte - b'0'))
        });
        match value.filter(|value| *value <= 65535) {
            Some(value) => Ok(Some(value as u16)),
            None => Err(SourceFailure::value("Port out of range 0-65535")),
        }
    }
}

pub fn split(value: &str) -> SourceResult<Parts> {
    let value: String = value
        .trim_start_matches(|ch: char| ch <= '\u{20}')
        .chars()
        .filter(|ch| !['\t', '\r', '\n'].contains(ch))
        .collect();
    let mut remaining = value.as_str();
    let mut scheme = String::new();
    if let Some(colon) = remaining.find(':') {
        let prefix = &remaining[..colon];
        if prefix
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphabetic)
            && prefix
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"+.-".contains(&byte))
        {
            scheme = prefix.to_ascii_lowercase();
            remaining = &remaining[colon + 1..];
        }
    }
    let mut netloc = "";
    if let Some(after) = remaining.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
        remaining = &after[end..];
        if netloc.contains('[') != netloc.contains(']') {
            return Err(SourceFailure::value("Invalid IPv6 URL"));
        }
        if netloc.contains('[') && netloc.contains(']') {
            bracketed_netloc(netloc)?;
        }
    }
    let (remaining, fragment) = remaining.split_once('#').unwrap_or((remaining, ""));
    let (path, query) = remaining.split_once('?').unwrap_or((remaining, ""));
    // CPython's NFKC check removes existing @ : # ? then rejects compatibility
    // decompositions introducing / ? # @ :. These are the Unicode 15.1 scalar
    // decompositions relevant to those five ASCII delimiters; no IDNA folding
    // or normalization is otherwise applied to the hostname.
    if netloc.chars().any(nfkc_delimiter) {
        return Err(SourceFailure::value(format!(
            "netloc '{netloc}' contains invalid characters under NFKC normalization"
        )));
    }
    let (hostinfo, userinfo) = netloc
        .rsplit_once('@')
        .map_or((netloc, false), |(_, host)| (host, true));
    let (host, port) = if let Some((_, bracketed)) = hostinfo.split_once('[') {
        let (host, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
        let port = after.split_once(':').map(|(_, port)| port);
        (host, port)
    } else {
        hostinfo
            .split_once(':')
            .map_or((hostinfo, None), |(host, port)| (host, Some(port)))
    };
    let hostname = if host.is_empty() {
        None
    } else {
        Some(host.split_once('%').map_or_else(
            || host.to_lowercase(),
            |(host, zone)| format!("{}%{zone}", host.to_lowercase()),
        ))
    };
    Ok(Parts {
        scheme,
        hostname,
        userinfo,
        path: path.into(),
        query: query.into(),
        fragment: fragment.into(),
        port: port.filter(|port| !port.is_empty()).map(str::to_owned),
    })
}

fn bracketed_netloc(netloc: &str) -> SourceResult<()> {
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, host)| host);
    let host = if let Some((before, bracketed)) = hostinfo.split_once('[') {
        if !before.is_empty() {
            return Err(SourceFailure::value("Invalid IPv6 URL"));
        }
        let (host, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
        if !after.is_empty() && !after.starts_with(':') {
            return Err(SourceFailure::value("Invalid IPv6 URL"));
        }
        host
    } else {
        hostinfo.split_once(':').map_or(hostinfo, |(host, _)| host)
    };
    if let Some(version) = host.strip_prefix('v') {
        if !version.split_once('.').is_some_and(|(version, address)| {
            !version.is_empty()
                && version.bytes().all(|byte| byte.is_ascii_hexdigit())
                && !address.is_empty()
        }) {
            return Err(SourceFailure::value("IPvFuture address is invalid"));
        }
    } else {
        if host.parse::<std::net::Ipv4Addr>().is_ok() {
            return Err(SourceFailure::value(
                "An IPv4 address cannot be in brackets",
            ));
        }
        let (address, zone) = host
            .split_once('%')
            .map_or((host, None), |(address, zone)| (address, Some(zone)));
        if address.parse::<std::net::Ipv6Addr>().is_err()
            || zone.is_some_and(|zone| zone.is_empty() || zone.contains('%'))
        {
            return Err(SourceFailure::value(format!(
                "{} does not appear to be an IPv4 or IPv6 address",
                pyrepr(&PyValue::String(host.into()))
            )));
        }
    }
    Ok(())
}

fn nfkc_delimiter(ch: char) -> bool {
    matches!(
        ch,
        '\u{2047}'
            | '\u{2048}'
            | '\u{2049}'
            | '\u{2100}'
            | '\u{2101}'
            | '\u{2105}'
            | '\u{2106}'
            | '\u{2a74}'
            | '\u{fe13}'
            | '\u{fe16}'
            | '\u{fe55}'
            | '\u{fe56}'
            | '\u{fe5f}'
            | '\u{fe6b}'
            | '\u{ff03}'
            | '\u{ff0f}'
            | '\u{ff1a}'
            | '\u{ff1f}'
            | '\u{ff20}'
    )
}
