//! Exercise the real Tauri ACL resolver without starting a native window.

#[path = "../src/signed_pack_panel.rs"]
mod signed_pack_panel;

use std::collections::BTreeMap;
use tauri::ipc::{Origin, RuntimeAuthority};
use tauri::utils::acl::{manifest::Manifest, resolved::Resolved};
use tauri::Url;

const PACK_COMMANDS: [&str; 3] = [
    "signed_pack_admission_status",
    "admit_signed_pack_from_folder",
    "onboard_signed_pack_from_folder",
];

fn empty_authority() -> RuntimeAuthority {
    let acl: BTreeMap<String, Manifest> = serde_json::from_str(include_str!(concat!(
        env!("OUT_DIR"),
        "/acl-manifests.json"
    )))
    .expect("the build-generated application permission manifest must parse");
    let resolved = Resolved::resolve(
        &acl,
        BTreeMap::new(),
        tauri::utils::platform::Target::current(),
    )
    .expect("the application manifest must resolve without capabilities");
    RuntimeAuthority::new(acl, resolved)
}

#[test]
fn runtime_pack_capability_and_guard_allow_only_the_selected_main_panel() {
    for port in [8765, 18765, 65535] {
        let mut authority = empty_authority();
        let panel_url = Url::parse(&format!("http://127.0.0.1:{port}/panel/")).unwrap();
        let panel_origin = Origin::Remote {
            url: panel_url.clone(),
        };
        for command in PACK_COMMANDS {
            assert!(authority
                .resolve_access(command, "main", "main", &panel_origin)
                .is_none());
        }

        authority
            .add_capability(signed_pack_panel::capability_document(port).unwrap())
            .expect("the startup-selected capability must resolve generated permissions");

        let other_port = if port == 8765 { 18765 } else { 8765 };
        let cases = [
            ("main", format!("http://127.0.0.1:{port}/panel"), true),
            ("main", format!("http://localhost:{port}/panel/"), true),
            (
                "main",
                format!("http://127.0.0.1:{port}/panel/packs?a=b#pack"),
                true,
            ),
            (
                "main",
                format!("http://127.0.0.1:{other_port}/panel/"),
                false,
            ),
            ("main", format!("http://127.0.0.1:{port}/approval"), false),
            ("main", format!("http://127.0.0.1:{port}/panelx"), false),
            (
                "main",
                format!("http://127.0.0.1:{port}/panel%2Fpacks"),
                false,
            ),
            (
                "main",
                format!("http://127.0.0.1:{port}/panel/%2e%2e/approval"),
                false,
            ),
            ("main", format!("https://127.0.0.1:{port}/panel/"), false),
            (
                "main",
                format!("http://example.invalid:{port}/panel/"),
                false,
            ),
            (
                "authority-approval",
                format!("http://127.0.0.1:{port}/panel/"),
                false,
            ),
            (
                "defaultspack-main",
                format!("http://127.0.0.1:{port}/panel/"),
                false,
            ),
        ];

        for command in PACK_COMMANDS {
            for (label, address, expected) in &cases {
                let url = Url::parse(address).unwrap();
                let origin = Origin::Remote { url: url.clone() };
                // A main-labelled webview must not widen a non-main window:
                // only the window selector is present in this capability.
                let origin_allowed = *label == "main"
                    && url.scheme() == "http"
                    && matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"))
                    && url.port_or_known_default() == Some(port);
                let acl_allowed = authority
                    .resolve_access(command, label, "main", &origin)
                    .is_some();
                assert_eq!(acl_allowed, origin_allowed, "transport origin: {address}");
                assert_eq!(
                    acl_allowed && signed_pack_panel::caller_allowed(label, &url, port),
                    *expected,
                    "{command}, {label}, {address}"
                );
                assert_eq!(
                    signed_pack_panel::caller_allowed(label, &url, port),
                    *expected,
                    "native caller guard: {label}, {address}"
                );
            }
            assert!(authority
                .resolve_access(command, "main", "main", &Origin::Local)
                .is_none());
        }

        for unrelated_command in [
            "reauthorize_panel_session",
            "restart_kernel",
            "plugin:dialog|open",
            "launch_selected_presentation",
        ] {
            assert!(authority
                .resolve_access(unrelated_command, "main", "main", &panel_origin)
                .is_none());
        }
    }
}

#[test]
fn custom_protocol_origin_and_live_window_path_are_separate_checks() {
    for port in [8765, 18765, 65535] {
        let mut authority = empty_authority();
        authority
            .add_capability(signed_pack_panel::capability_document(port).unwrap())
            .unwrap();
        for host in ["127.0.0.1", "localhost"] {
            for path in ["/panel", "/panel/packs", "/", "/approval", "/panelx"] {
                let live_url = Url::parse(&format!("http://{host}:{port}{path}")).unwrap();
                // Mirrors ipc/protocol.rs: the custom protocol supplies HTTP Origin,
                // which does not include the invoking document's path.
                let transport_url = Url::parse(&live_url.origin().ascii_serialization()).unwrap();
                assert_eq!(transport_url.path(), "/");
                for command in PACK_COMMANDS {
                    let origin = Origin::Remote {
                        url: transport_url.clone(),
                    };
                    assert!(authority
                        .resolve_access(command, "main", "main", &origin)
                        .is_some());
                    assert_eq!(
                        signed_pack_panel::caller_allowed("main", &live_url, port),
                        path == "/panel" || path.starts_with("/panel/")
                    );
                    assert!(!signed_pack_panel::caller_allowed(
                        "main",
                        &transport_url,
                        port
                    ));
                    for label in ["authority-approval", "defaultspack-main"] {
                        assert!(authority
                            .resolve_access(command, label, "main", &origin)
                            .is_none());
                    }
                }
            }
        }
        for address in [
            format!(
                "http://127.0.0.1:{}/",
                if port == 8765 { 18765 } else { 8765 }
            ),
            format!("https://localhost:{port}/"),
            format!("http://example.invalid:{port}/"),
        ] {
            let origin = Origin::Remote {
                url: Url::parse(&address).unwrap(),
            };
            for command in PACK_COMMANDS {
                assert!(authority
                    .resolve_access(command, "main", "main", &origin)
                    .is_none());
            }
        }
    }
}
