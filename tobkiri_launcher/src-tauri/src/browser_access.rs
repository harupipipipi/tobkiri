//! Launcher-owned browser admission, isolated from Pack tool authority.
use crate::config::AppConfig;
use hmac::{Hmac, Mac};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::Read;
use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::Manager;

use crate::browser_access_lifecycle::{
    generation_label, remaining_ttl, take_generation, take_window_generation,
};
const PAGE: &str = "browser-access-approval.html";
#[derive(Clone, Deserialize, Serialize)]
pub(crate) struct Context {
    request_id: String,
    origin: String,
    target: String,
    profile_id: String,
    expires_in: u64,
}
#[derive(Clone)]
struct Pending {
    context: Context,
    port: u16,
    nonce: String,
    label: String,
    deadline: Instant,
    settling: bool,
}
#[derive(Default)]
pub(crate) struct Coordinator(Mutex<HashMap<String, Pending>>);

fn matches_origin(origin: &str, port: u16) -> bool {
    origin == format!("http://127.0.0.1:{port}") || origin == format!("http://localhost:{port}")
}
fn validate_context(context: &Context, request_id: &str, port: u16) -> Result<(), String> {
    if !crate::valid_authority_request_id(request_id)
        || context.request_id != request_id
        || !matches_origin(&context.origin, port)
        || context.profile_id.is_empty()
        || !context
            .profile_id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
        || context.target != format!("/p/{}/chat", context.profile_id)
        || context.target.contains(['?', '#', '\\'])
        || context
            .target
            .split('/')
            .any(|part| part == "." || part == "..")
        || context.expires_in == 0
        || context.expires_in > 300
    {
        return Err("invalid browser access context".into());
    }
    Ok(())
}
fn proof_message(
    kind: &str,
    timestamp: &str,
    nonce: &str,
    path: &str,
    status: Option<u16>,
    bytes: &[u8],
) -> String {
    let digest = hex::encode(Sha256::digest(bytes));
    match status {
        Some(status) => format!("tobkiri.browser-access.{kind}.v1\n{timestamp}\n{nonce}\nPOST\n{path}\n{status}\n{digest}"),
        None => format!("tobkiri.browser-access.{kind}.v1\n{timestamp}\n{nonce}\nPOST\n{path}\n{digest}"),
    }
}
fn sign(secret: &str, message: &str) -> Result<String, String> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(secret.as_bytes()).map_err(|_| "proof unavailable")?;
    mac.update(message.as_bytes());
    Ok(hex::encode(mac.finalize().into_bytes()))
}
fn verify(secret: &str, message: &str, proof: &str) -> Result<(), String> {
    if proof.len() != 64
        || !proof
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("invalid Host proof".into());
    }
    let bytes = hex::decode(proof).map_err(|_| "invalid Host proof")?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(secret.as_bytes()).map_err(|_| "proof unavailable")?;
    mac.update(message.as_bytes());
    mac.verify_slice(&bytes)
        .map_err(|_| "invalid Host proof".into())
}
fn host_call(config: &AppConfig, port: u16, endpoint: &str, body: Value) -> Result<Value, String> {
    let secret = crate::load_or_create_panel_bootstrap_secret(config)
        .map_err(|_| "bootstrap unavailable")?;
    authenticated_call(&secret, port, endpoint, body)
}
fn authenticated_call(
    secret: &str,
    port: u16,
    endpoint: &str,
    body: Value,
) -> Result<Value, String> {
    if endpoint != "context" && endpoint != "decision" {
        return Err("invalid endpoint".into());
    }
    let path = format!("/api/panel/browser-access/{endpoint}");
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "clock unavailable")?
        .as_secs()
        .to_string();
    let mut random = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut random);
    let nonce = hex::encode(random);
    let bytes = serde_json::to_vec(&body).map_err(|_| "invalid request")?;
    let proof = sign(
        secret,
        &proof_message("request", &timestamp, &nonce, &path, None, &bytes),
    )?;
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(5))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
        .map_err(|_| "transport unavailable")?;
    let response = client
        .post(format!("http://127.0.0.1:{port}{path}"))
        .header("Content-Type", "application/json")
        .header("X-Tobkiri-Browser-Access-Timestamp", &timestamp)
        .header("X-Tobkiri-Browser-Access-Nonce", &nonce)
        .header("X-Tobkiri-Browser-Access-Proof", proof)
        .body(bytes)
        .send()
        .map_err(|_| "Host unavailable")?;
    let status = response.status();
    let response_proof = response
        .headers()
        .get("X-Tobkiri-Browser-Access-Response-Proof")
        .and_then(|v| v.to_str().ok())
        .ok_or("missing Host proof")?
        .to_owned();
    let mut response_bytes = Vec::new();
    response
        .take(65537)
        .read_to_end(&mut response_bytes)
        .map_err(|_| "invalid Host response")?;
    if response_bytes.len() > 65536 {
        return Err("Host response too large".into());
    }
    verify(
        secret,
        &proof_message(
            "response",
            &timestamp,
            &nonce,
            &path,
            Some(status.as_u16()),
            &response_bytes,
        ),
        &response_proof,
    )?;
    if !status.is_success() {
        return Err("Host rejected browser access".into());
    }
    let envelope: Value =
        serde_json::from_slice(&response_bytes).map_err(|_| "invalid Host response")?;
    if envelope.get("success").and_then(Value::as_bool) != Some(true) {
        return Err("Host rejected browser access".into());
    }
    Ok(envelope.get("data").cloned().unwrap_or(Value::Null))
}
pub(crate) fn open(
    app: &tauri::AppHandle,
    config: &AppConfig,
    request_id: &str,
    port: u16,
) -> Result<(), String> {
    if port != crate::active_defaultspack_http_port()
        || !crate::valid_authority_request_id(request_id)
    {
        return Err("invalid browser access request".into());
    }
    let context: Context = serde_json::from_value(host_call(
        config,
        port,
        "context",
        json!({"request_id": request_id}),
    )?)
    .map_err(|_| "invalid browser access context")?;
    validate_context(&context, request_id, port)?;
    let coordinator = app.state::<Coordinator>();
    let mut records = coordinator
        .0
        .lock()
        .map_err(|_| "coordinator unavailable")?;
    if records.contains_key(request_id) {
        return Ok(());
    }
    if !records.is_empty() {
        return Err("browser approval already open".into());
    }
    let mut bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut bytes);
    let nonce: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    let label = generation_label(&nonce);
    let deadline = Instant::now() + Duration::from_secs(context.expires_in);
    records.insert(
        request_id.into(),
        Pending {
            settling: false,
            deadline,
            context,
            port,
            nonce: nonce.clone(),
            label: label.clone(),
        },
    );
    drop(records);
    let app_ui = app.clone();
    let uri = format!("{PAGE}?request_id={request_id}&nonce={nonce}");
    let opened = crate::run_ui_work_on_main_thread(app, "browser approval", move || {
        crate::activate_app_for_authority_approval();
        tauri::WebviewWindowBuilder::new(&app_ui, &label, tauri::WebviewUrl::App(uri.into()))
            .title("Tobkiri ブラウザーアクセス許可")
            .inner_size(480.0, 420.0)
            .incognito(true)
            .focused(true)
            .build()
            .map(|_| ())
            .map_err(|_| "approval window unavailable".into())
    });
    if opened.is_err() {
        close_generation(app, config, request_id, &nonce);
        return opened;
    }
    let app_expiry = app.clone();
    let config_expiry = config.clone();
    let expiration_request_id = request_id.to_owned();
    std::thread::spawn(move || {
        std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
        close_generation(&app_expiry, &config_expiry, &expiration_request_id, &nonce);
    });
    opened
}
fn caller_matches(
    label: &str,
    url: &tauri::Url,
    focused: bool,
    pending: &Pending,
    request_id: &str,
    nonce: &str,
) -> bool {
    let bundled = (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (url.scheme() == "http"
            && url.host_str() == Some("tauri.localhost")
            && url.port_or_known_default() == Some(80));
    bundled
        && label == pending.label
        && focused
        && url.path() == format!("/{PAGE}")
        && url
            .query_pairs()
            .any(|(k, v)| k == "request_id" && v == request_id)
        && url.query_pairs().any(|(k, v)| k == "nonce" && v == nonce)
        && !pending.settling
        && pending.context.request_id == request_id
        && pending.nonce == nonce
        && Instant::now() < pending.deadline
}
fn inspect(
    app: &tauri::AppHandle,
    window: &tauri::WebviewWindow,
    request_id: &str,
    nonce: &str,
) -> Result<Pending, String> {
    let window_ui = window.clone();
    let (label, url, focused) =
        crate::run_ui_work_on_main_thread(app, "browser approval caller", move || {
            Ok((
                window_ui.label().to_owned(),
                window_ui.url().map_err(|_| "caller unavailable")?,
                window_ui.is_focused().map_err(|_| "caller unavailable")?,
            ))
        })?;
    let pending = app
        .state::<Coordinator>()
        .0
        .lock()
        .map_err(|_| "coordinator unavailable")?
        .get(request_id)
        .cloned()
        .ok_or("request unavailable")?;
    if !caller_matches(&label, &url, focused, &pending, request_id, nonce) {
        return Err("invalid approval caller".into());
    }
    Ok(pending)
}
#[tauri::command]
pub(crate) async fn browser_access_context(
    app: tauri::AppHandle,
    window: tauri::WebviewWindow,
    request_id: String,
    nonce: String,
) -> Result<Context, String> {
    let pending = inspect(&app, &window, &request_id, &nonce)?;
    let remaining = remaining_ttl(pending.deadline, Instant::now()).ok_or("request expired")?;
    let mut context = pending.context;
    context.expires_in = remaining;
    Ok(context)
}
#[tauri::command]
pub(crate) async fn browser_access_decide(
    app: tauri::AppHandle,
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
    request_id: String,
    nonce: String,
    decision: String,
) -> Result<(), String> {
    if decision != "approved" && decision != "denied" {
        return Err("invalid decision".into());
    }
    let pending = inspect(&app, &window, &request_id, &nonce)?;
    {
        let coordinator = app.state::<Coordinator>();
        let mut records = coordinator
            .0
            .lock()
            .map_err(|_| "coordinator unavailable")?;
        let record = records.get_mut(&request_id).ok_or("request unavailable")?;
        if record.settling || record.nonce != nonce || Instant::now() >= record.deadline {
            return Err("request already settled".into());
        }
        record.settling = true;
    }
    let config_owned = config.inner().clone();
    let id = request_id.clone();
    let decision_port = pending.port;
    let result = tauri::async_runtime::spawn_blocking(move || {
        host_call(
            &config_owned,
            decision_port,
            "decision",
            json!({"request_id": id, "decision": decision}),
        )
    })
    .await;
    match result {
        Ok(Ok(_)) => {
            let coordinator = app.state::<Coordinator>();
            let mut records = coordinator
                .0
                .lock()
                .map_err(|_| "coordinator unavailable")?;
            take_generation(&mut records, &request_id, &nonce, |p| &p.nonce);
        }
        _ => {
            close_generation(&app, config.inner(), &request_id, &nonce);
            return Err("decision unavailable".into());
        }
    }
    let app_ui = app.clone();
    let label = pending.label;
    crate::run_ui_work_on_main_thread(&app, "close browser approval", move || {
        if let Some(window) = app_ui.get_webview_window(&label) {
            window.close().map_err(|_| "close failed".into())
        } else {
            Ok(())
        }
    })
}
/// Resolve a close event only for the exact controlled window generation.
pub(crate) fn close_window(app: &tauri::AppHandle, config: &AppConfig, label: &str) {
    let pending = app
        .state::<Coordinator>()
        .0
        .lock()
        .ok()
        .and_then(|mut r| take_window_generation(&mut r, label, |p| &p.label));
    cleanup_pending(app, config, pending);
}

fn close_generation(app: &tauri::AppHandle, config: &AppConfig, request_id: &str, nonce: &str) {
    let pending = app
        .state::<Coordinator>()
        .0
        .lock()
        .ok()
        .and_then(|mut r| take_generation(&mut r, request_id, nonce, |p| &p.nonce));
    cleanup_pending(app, config, pending);
}

fn cleanup_pending(app: &tauri::AppHandle, config: &AppConfig, pending: Option<Pending>) {
    if let Some(pending) = pending {
        let app_ui = app.clone();
        let label = pending.label.clone();
        let _ = app.run_on_main_thread(move || {
            if let Some(window) = app_ui.get_webview_window(&label) {
                let _ = window.close();
            }
        });
        let config = config.clone();
        std::thread::spawn(move || {
            let _ = host_call(
                &config,
                pending.port,
                "decision",
                json!({"request_id":pending.context.request_id,"decision":"denied"}),
            );
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn request_proof_matches_protocol_vector_and_fences_response_fields() {
        let body = br#"{"request_id":"req_1"}"#;
        let nonce = "0".repeat(64);
        let path = "/api/panel/browser-access/context";
        let request = proof_message("request", "1700000000", &nonce, path, None, body);
        assert_eq!(
            sign("test-bootstrap-key", &request).unwrap(),
            "d1968b1822947a0715fcd3ed600f2e57286256f245b28746c927c76c473cd672"
        );
        let response = proof_message("response", "1700000000", &nonce, path, Some(200), body);
        let proof = sign("test-bootstrap-key", &response).unwrap();
        assert!(verify("test-bootstrap-key", &response, &proof).is_ok());
        assert!(verify("other-key", &response, &proof).is_err());
        assert!(verify(
            "test-bootstrap-key",
            &proof_message("response", "1700000000", &nonce, path, Some(401), body),
            &proof
        )
        .is_err());
        assert!(verify("test-bootstrap-key", &request, &proof).is_err());
    }
    #[test]
    fn replacement_listener_never_receives_bootstrap_secret_or_forges_context() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        for forged_header in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            let server = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut request = Vec::new();
                let mut chunk = [0u8; 1024];
                loop {
                    let read = stream.read(&mut chunk).unwrap();
                    assert!(read > 0);
                    request.extend_from_slice(&chunk[..read]);
                    assert!(request.len() < 8192);
                    if let Some(end) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                        let headers = String::from_utf8_lossy(&request[..end]).to_ascii_lowercase();
                        let length: usize = headers
                            .lines()
                            .find_map(|line| line.strip_prefix("content-length: "))
                            .unwrap()
                            .parse()
                            .unwrap();
                        if request.len() >= end + 4 + length {
                            break;
                        }
                    }
                }
                let text = String::from_utf8(request).unwrap();
                assert!(!text.contains("private-bootstrap-test-secret"));
                assert!(!text
                    .to_ascii_lowercase()
                    .contains("x-rumi-desktop-bootstrap"));
                assert!(text
                    .to_ascii_lowercase()
                    .contains("x-tobkiri-browser-access-proof:"));
                let body = br#"{"success":true,"data":{"request_id":"forged"}}"#;
                let header = if forged_header {
                    format!(
                        "X-Tobkiri-Browser-Access-Response-Proof: {}\r\n",
                        "0".repeat(64)
                    )
                } else {
                    String::new()
                };
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n{header}Connection: close\r\n\r\n",
                    body.len()
                )
                .unwrap();
                stream.write_all(body).unwrap();
            });
            assert!(authenticated_call(
                "private-bootstrap-test-secret",
                port,
                "context",
                json!({"request_id":"req_1"})
            )
            .is_err());
            server.join().unwrap();
        }
    }
    fn context() -> Context {
        Context {
            request_id: "req_1".into(),
            origin: "http://127.0.0.1:8766".into(),
            target: "/p/defaults/chat".into(),
            profile_id: "defaults".into(),
            expires_in: 120,
        }
    }
    #[test]
    fn context_pins_origin_port_request_and_target() {
        let c = context();
        assert!(validate_context(&c, "req_1", 8766).is_ok());
        assert!(validate_context(&c, "other", 8766).is_err());
        assert!(validate_context(&c, "req_1", 8767).is_err());
        let mut c = c;
        c.target = "//evil.example".into();
        assert!(validate_context(&c, "req_1", 8766).is_err());
    }
    #[test]
    fn caller_requires_bundled_focused_window_and_nonce() {
        let p = Pending {
            context: context(),
            port: 8766,
            nonce: "nonce".into(),
            label: generation_label("nonce"),
            settling: false,
            deadline: Instant::now() + Duration::from_secs(60),
        };
        let url = tauri::Url::parse(
            "tauri://localhost/browser-access-approval.html?request_id=req_1&nonce=nonce",
        )
        .unwrap();
        assert!(caller_matches(&p.label, &url, true, &p, "req_1", "nonce"));
        assert!(!caller_matches("main", &url, true, &p, "req_1", "nonce"));
        assert!(!caller_matches(
            &generation_label("other"),
            &url,
            true,
            &p,
            "req_1",
            "nonce"
        ));
        assert!(!caller_matches(&p.label, &url, false, &p, "req_1", "nonce"));
        assert!(!caller_matches(&p.label, &url, true, &p, "req_1", "wrong"));
        let remote = tauri::Url::parse(
            "http://127.0.0.1:8766/browser-access-approval.html?request_id=req_1&nonce=nonce",
        )
        .unwrap();
        assert!(!caller_matches(
            &p.label, &remote, true, &p, "req_1", "nonce"
        ));
    }
}
