use url::Url;

/// Every isolated document retains its own opaque sandbox origin.
pub const PACK_UI_CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors tauri://localhost http://tauri.localhost https://tauri.localhost";

/// Bounded, non-secret errors suitable for an untrusted frame.
#[derive(Debug, thiserror::Error)]
pub enum HostError {
    #[error("this command is available only to the trusted Launcher host")]
    Caller,
    #[error("invalid isolated UI asset path")]
    AssetPath,
    #[error("no Core Session is running")]
    NoSession,
    #[error("invalid UI request")]
    Invalid,
    #[error("UI request limit exceeded")]
    Limit,
    #[error("UI operation registration capacity is full")]
    RegistrationFull,
    #[error("UI operation was already claimed or its ID was retired")]
    Duplicate,
    #[error("UI request cancelled")]
    Cancelled,
    #[error("Core refused the UI request")]
    Core,
    #[error("Core refused the UI request")]
    Rejected(&'static str),
}

/// Origin checking supplements Tauri's capability and frame invoke-key checks.
/// It cannot replace native iframe-denial verification on each platform.
pub fn trusted_host_origin(url: &str) -> bool {
    let Ok(url) = Url::parse(url) else {
        return false;
    };
    matches!(url.scheme(), "tauri" | "http" | "https")
        && url.host_str()
            == Some(if url.scheme() == "tauri" {
                "localhost"
            } else {
                "tauri.localhost"
            })
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
}

/// The native integration constructs this from the actual caller Webview.
pub struct HostCaller<'a> {
    pub label: &'a str,
    pub url: &'a str,
}

impl HostCaller<'_> {
    pub fn check(&self) -> Result<(), HostError> {
        if self.label == "core-host" && trusted_host_origin(self.url) {
            Ok(())
        } else {
            Err(HostError::Caller)
        }
    }
}

/// An exact path inside Core's verified file map, never an OS filesystem path.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackAssetPath(String);

/// Canonical per-platform Pack scheme admitted by the request parser. The
/// https Windows form still parses for compatibility but is never the
/// canonical representation `packUiUrl` emits, so it earns no positive
/// policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PackScheme {
    Native,
    WindowsHttp,
    WindowsHttps,
}

/// Platform-mapped custom protocol URL. Parsing retains the raw path so a URL
/// library cannot silently normalize traversal into an admitted map key.
pub struct PackAssetRequest {
    pub hash_hex: String,
    pub path: PackAssetPath,
    scheme: PackScheme,
}

impl PackAssetRequest {
    /// Per-response CSP bound to this exact verified Pack request. The
    /// sandboxed document's opaque origin makes `'self'` unusable for
    /// `script-src`/`style-src`, so both directives pin the exact
    /// lowercase-hash Pack source with its trailing-slash boundary. Every
    /// other directive and the parent frame-ancestors allowlist stay
    /// identical to [`PACK_UI_CSP`]. Noncanonical representations earn no
    /// positive policy.
    pub(crate) fn pack_ui_policy(&self) -> Result<String, HostError> {
        let source = match self.scheme {
            PackScheme::Native => format!("tobkiri-pack://{}/", self.hash_hex),
            PackScheme::WindowsHttp => {
                format!("http://tobkiri-pack.localhost/{}/", self.hash_hex)
            }
            PackScheme::WindowsHttps => return Err(HostError::AssetPath),
        };
        Ok(PACK_UI_CSP
            .replacen("script-src 'self'", &format!("script-src {source}"), 1)
            .replacen("style-src 'self'", &format!("style-src {source}"), 1))
    }

    pub fn parse(uri: &str) -> Result<Self, HostError> {
        if uri.len() > 16_384 {
            return Err(HostError::AssetPath);
        }
        let (scheme, tail) = if let Some(tail) = uri.strip_prefix("tobkiri-pack://") {
            (PackScheme::Native, tail)
        } else if let Some(tail) = uri.strip_prefix("http://tobkiri-pack.localhost/") {
            (PackScheme::WindowsHttp, tail)
        } else if let Some(tail) = uri.strip_prefix("https://tobkiri-pack.localhost/") {
            (PackScheme::WindowsHttps, tail)
        } else {
            return Err(HostError::AssetPath);
        };
        let (hash, path) = tail.split_once('/').ok_or(HostError::AssetPath)?;
        if hash.len() != 64
            || !hash
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(HostError::AssetPath);
        }
        if path.contains(['?', '#']) {
            return Err(HostError::AssetPath);
        }
        let mut decoded = Vec::new();
        for segment in path.split('/') {
            let mut bytes = Vec::new();
            let raw = segment.as_bytes();
            let mut index = 0;
            while index < raw.len() {
                if raw[index] == b'%' {
                    let hex = raw.get(index + 1..index + 3).ok_or(HostError::AssetPath)?;
                    let hex = std::str::from_utf8(hex).map_err(|_| HostError::AssetPath)?;
                    bytes.push(u8::from_str_radix(hex, 16).map_err(|_| HostError::AssetPath)?);
                    index += 3;
                } else {
                    bytes.push(raw[index]);
                    index += 1;
                }
            }
            let segment = String::from_utf8(bytes).map_err(|_| HostError::AssetPath)?;
            if segment.contains('/') {
                return Err(HostError::AssetPath);
            }
            decoded.push(segment);
        }
        Ok(Self {
            hash_hex: hash.into(),
            // Decode once, then use the exact verified map key. Encoded slashes,
            // traversal and invalid UTF-8 never become a normalized path.
            path: PackAssetPath::parse(&decoded.join("/"))?,
            scheme,
        })
    }
}

impl PackAssetPath {
    pub fn parse(path: &str) -> Result<Self, HostError> {
        if path.is_empty()
            || path.len() > 4096
            || path.starts_with('/')
            || path
                .bytes()
                .any(|b| b < 32 || b == 127 || matches!(b, b'\\' | b':'))
            || path
                .split('/')
                .any(|p| p.is_empty() || p == "." || p == "..")
        {
            return Err(HostError::AssetPath);
        }
        Ok(Self(path.into()))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
    pub fn mime(&self) -> Option<&'static str> {
        match self.0.rsplit('.').next()? {
            "html" => Some("text/html; charset=utf-8"),
            "js" | "mjs" => Some("text/javascript; charset=utf-8"),
            "css" => Some("text/css; charset=utf-8"),
            "json" => Some("application/json"),
            "png" => Some("image/png"),
            "jpg" | "jpeg" => Some("image/jpeg"),
            "gif" => Some("image/gif"),
            "webp" => Some("image/webp"),
            "svg" => Some("image/svg+xml"),
            "woff" => Some("font/woff"),
            "woff2" => Some("font/woff2"),
            "ico" => Some("image/x-icon"),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remote_loopback_pack_and_lookalike_origins_cannot_invoke_host() {
        for origin in [
            "null",
            "http://localhost:3000/",
            "http://127.0.0.1/",
            "tobkiri-pack://abc/ui.html",
            "https://tauri.localhost.evil/",
            "https://tauri.localhost:444/",
            "tauri://user@localhost/",
            "file:///ui.html",
        ] {
            assert!(!trusted_host_origin(origin), "{origin}");
        }
        for origin in [
            "tauri://localhost/",
            "http://tauri.localhost/index.html",
            "https://tauri.localhost/",
        ] {
            assert!(trusted_host_origin(origin));
            assert!(
                HostCaller {
                    label: "core-host",
                    url: origin
                }
                .check()
                .is_ok()
            );
            assert!(
                HostCaller {
                    label: "main",
                    url: origin
                }
                .check()
                .is_err()
            );
        }
    }

    #[test]
    fn assets_are_exact_map_keys_without_url_or_filesystem_normalization() {
        for path in [
            "",
            "/ui/index.html",
            "ui/../pack.json",
            "ui/./index.html",
            "ui//index.html",
            "ui\\key",
            "C:/key",
            "ui/\0secret",
        ] {
            assert!(PackAssetPath::parse(path).is_err(), "{path:?}");
        }
        let path = PackAssetPath::parse("ui/assets/application.mjs").unwrap();
        assert_eq!(path.as_str(), "ui/assets/application.mjs");
        assert_eq!(path.mime(), Some("text/javascript; charset=utf-8"));
        assert!(PackAssetPath::parse("pack.wasm").unwrap().mime().is_none());
        let hash = "a".repeat(64);
        for uri in [
            format!("tobkiri-pack://{hash}/ui/index.html"),
            format!("http://tobkiri-pack.localhost/{hash}/ui/index.html"),
        ] {
            let request = PackAssetRequest::parse(&uri).unwrap();
            assert_eq!(request.hash_hex, hash);
            assert_eq!(request.path.as_str(), "ui/index.html");
        }
        for uri in [
            format!("tobkiri-pack://{hash}/ui/../pack.json"),
            format!("tobkiri-pack://{hash}/ui/%2e%2e/pack.json"),
            format!("http://tobkiri-pack.localhost.evil/{hash}/ui/index.html"),
        ] {
            assert!(PackAssetRequest::parse(&uri).is_err());
        }
        let unicode = PackAssetRequest::parse(&format!(
            "tobkiri-pack://{hash}/ui/%E6%97%A5%E6%9C%AC%20name.js"
        ))
        .unwrap();
        assert_eq!(unicode.path.as_str(), "ui/日本 name.js");
        let literal =
            PackAssetRequest::parse(&format!("tobkiri-pack://{hash}/ui/%252e%252e.js")).unwrap();
        assert_eq!(literal.path.as_str(), "ui/%2e%2e.js");
        assert!(PackAssetRequest::parse(&format!("tobkiri-pack://{hash}/ui/a%2fb.js")).is_err());
    }

    #[test]
    fn pack_policy_pins_exact_verified_source_per_canonical_scheme() {
        let hash = "0123456789abcdef".repeat(4);
        let native = PackAssetRequest::parse(&format!("tobkiri-pack://{hash}/ui/app.js")).unwrap();
        let windows =
            PackAssetRequest::parse(&format!("http://tobkiri-pack.localhost/{hash}/ui/app.js"))
                .unwrap();
        let https =
            PackAssetRequest::parse(&format!("https://tobkiri-pack.localhost/{hash}/ui/app.js"))
                .unwrap();
        for (request, expected_source) in [
            (&native, format!("tobkiri-pack://{hash}/")),
            (&windows, format!("http://tobkiri-pack.localhost/{hash}/")),
        ] {
            let policy = request.pack_ui_policy().unwrap();
            let mut directives = std::collections::BTreeMap::new();
            for directive in policy.split(';').map(str::trim) {
                let mut words = directive.split_whitespace();
                let name = words.next().expect("nonempty CSP directive");
                assert!(
                    directives.insert(name, words.collect::<Vec<_>>()).is_none(),
                    "duplicate CSP directive"
                );
            }
            // Independent intended policy, rather than production's replacen
            // algorithm or PACK_UI_CSP as the expected-value oracle.
            for name in ["script-src", "style-src"] {
                let sources = directives.remove(name).expect("executable asset directive");
                assert_eq!(sources, [expected_source.as_str()]);
                assert!(sources[0].ends_with('/'));
                assert!(!sources[0].contains('*'));
                assert!(!sources[0].contains(&"b".repeat(64)));
            }
            assert_eq!(
                directives,
                std::collections::BTreeMap::from([
                    ("default-src", vec!["'self'"]),
                    ("img-src", vec!["'self'", "data:"]),
                    ("font-src", vec!["'self'"]),
                    ("connect-src", vec!["'none'"]),
                    ("frame-src", vec!["'none'"]),
                    ("worker-src", vec!["'none'"]),
                    ("object-src", vec!["'none'"]),
                    ("base-uri", vec!["'none'"]),
                    ("form-action", vec!["'none'"]),
                    (
                        "frame-ancestors",
                        vec![
                            "tauri://localhost",
                            "http://tauri.localhost",
                            "https://tauri.localhost"
                        ],
                    ),
                ])
            );
            assert!(!policy.contains("'unsafe-inline'"));
            assert!(!policy.contains("'unsafe-eval'"));
        }
        // packUiUrl never emits the https Windows form; noncanonical input
        // earns no positive policy.
        assert!(https.pack_ui_policy().is_err());
        // The trailing-slash source binds this exact hash only.
        let other =
            PackAssetRequest::parse(&format!("tobkiri-pack://{}/ui/app.js", "b".repeat(64)))
                .unwrap();
        assert!(!other.pack_ui_policy().unwrap().contains(hash.as_str()));
    }

    #[test]
    fn parser_still_denies_ambiguous_authority_and_path_forms() {
        let hash = "a".repeat(64);
        for uri in [
            format!("tobkiri-pack://user@{hash}/ui/index.html"),
            format!("tobkiri-pack://{hash}:443/ui/index.html"),
            format!("tobkiri-pack://{}/ui/index.html", hash.to_uppercase()),
            format!("http://user:pw@tobkiri-pack.localhost/{hash}/ui/index.html"),
            format!("http://tobkiri-pack.localhost:8080/{hash}/ui/index.html"),
            format!("http://{hash}.tobkiri-pack.localhost/ui/index.html"),
            format!("http://tobkiri-pack.localhost/{hash}/ui/index.html?x=1"),
            format!("http://tobkiri-pack.localhost/{hash}/ui/index.html#f"),
        ] {
            assert!(PackAssetRequest::parse(&uri).is_err(), "{uri}");
        }
    }
}
