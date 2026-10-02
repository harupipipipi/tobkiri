//! Bind native Pack pickers to this startup's exact Host panel origin.

use tauri::Url;

pub(crate) fn capability_document(port: u16) -> Option<String> {
    if port == 0 {
        return None;
    }
    // Custom-protocol IPC carries HTTP Origin; WK postMessage carries the full
    // frame URL. Grant only this startup's origin and keep route enforcement in
    // the live native caller guard below, independently of transport metadata.
    let urls: Vec<String> = ["127.0.0.1", "localhost"]
        .into_iter()
        .map(|host| format!("http://{host}:{port}/*"))
        .collect();
    Some(
        serde_json::json!({
            "identifier": "launcher-signed-pack-current-panel",
            "description": "Native Pack admission for the selected local Host panel only.",
            "local": false,
            "windows": ["main"],
            "remote": {"urls": urls},
            "permissions": [
                "allow-signed-pack-admission-status",
                "allow-admit-signed-pack-from-folder",
                "allow-onboard-signed-pack-from-folder"
            ]
        })
        .to_string(),
    )
}

pub(crate) fn caller_allowed(label: &str, url: &Url, port: u16) -> bool {
    label == "main"
        && port != 0
        && url.scheme() == "http"
        && matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"))
        && url.username().is_empty()
        && url.password().is_none()
        && url.port_or_known_default() == Some(port)
        && (url.path() == "/panel" || url.path().starts_with("/panel/"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capability_is_exact_port_main_panel_and_three_commands_only() {
        for port in [8765, 18765, 65535] {
            let value: serde_json::Value =
                serde_json::from_str(&capability_document(port).unwrap()).unwrap();
            assert_eq!(value["local"], false);
            assert_eq!(value["windows"], serde_json::json!(["main"]));
            assert_eq!(
                value["permissions"],
                serde_json::json!([
                    "allow-signed-pack-admission-status",
                    "allow-admit-signed-pack-from-folder",
                    "allow-onboard-signed-pack-from-folder"
                ])
            );
            assert_eq!(
                value["remote"]["urls"],
                serde_json::json!([
                    format!("http://127.0.0.1:{port}/*"),
                    format!("http://localhost:{port}/*")
                ])
            );
        }
        assert!(capability_document(0).is_none());
    }

    #[test]
    fn current_panel_accepts_routes_without_using_query_as_authority() {
        for address in [
            "http://127.0.0.1:18765/panel",
            "http://127.0.0.1:18765/panel/packs?code=fixture#section",
            "http://localhost:18765/panel/",
        ] {
            assert!(caller_allowed("main", &Url::parse(address).unwrap(), 18765));
        }
    }

    #[test]
    fn foreign_origins_ports_routes_and_windows_fail_closed() {
        for address in [
            "http://127.0.0.1:8765/panel/",
            "http://127.0.0.1:18766/panel/",
            "https://127.0.0.1:18765/panel/",
            "http://example.invalid:18765/panel/",
            "http://localhost.example.invalid:18765/panel/",
            "http://127.0.0.1:18765/panel-other",
            "http://127.0.0.1:18765/",
            "http://127.0.0.1:18765/approval",
            "http://127.0.0.1:18765/panel/../approval",
            "http://user@127.0.0.1:18765/panel/",
            "http://user:password@localhost:18765/panel/",
            "tauri://localhost/panel/",
        ] {
            assert!(
                !caller_allowed("main", &Url::parse(address).unwrap(), 18765),
                "{address}"
            );
        }
        let panel = Url::parse("http://127.0.0.1:18765/panel/").unwrap();
        for label in ["authority-approval", "defaultspack-main", "other", ""] {
            assert!(!caller_allowed(label, &panel, 18765));
        }
        assert!(!caller_allowed("main", &panel, 0));
    }
}
