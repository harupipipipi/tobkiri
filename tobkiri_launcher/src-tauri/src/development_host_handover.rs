//! Native-only review of an exact development Host storage/data successor.

use crate::config::AppConfig;
use crate::{load_or_create_panel_bootstrap_secret, validate_launcher_main_window, ApiEnvelope};
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use std::io::Read;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

fn consent_message(
    action: &str,
    plan_digest: &str,
    nonce: &str,
    issued: u64,
    expires: u64,
) -> String {
    format!("v1\ntobkiri.development-host-handover\nmain\n{action}\n{plan_digest}\n{nonce}\n{issued}\n{expires}")
}

fn sign_consent(
    secret: &str,
    action: &str,
    plan_digest: &str,
    nonce: &str,
    now: u64,
) -> Result<Value, String> {
    if secret.is_empty()
        || !matches!(action, "commit" | "recover")
        || !plan_digest.starts_with("sha256:")
        || plan_digest.len() != 71
        || !plan_digest[7..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || nonce.len() < 32
        || nonce.len() > 128
        || !nonce
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err("Host handover review identity is invalid".into());
    }
    let expires = now
        .checked_add(180)
        .ok_or("Host handover clock is invalid")?;
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .map_err(|_| "Host handover signing is unavailable")?;
    mac.update(consent_message(action, plan_digest, nonce, now, expires).as_bytes());
    Ok(
        json!({"version": 1, "window_label": "main", "action": action, "plan_digest": plan_digest, "ceremony_nonce": nonce, "issued_at": now, "expires_at": expires, "signature": hex::encode(mac.finalize().into_bytes())}),
    )
}

fn read_result(response: reqwest::blocking::Response) -> Result<Value, String> {
    let success = response.status().is_success();
    let mut bytes = Vec::new();
    response
        .take(256 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "Host handover response is unavailable")?;
    if bytes.len() > 256 * 1024 {
        return Err("Host handover response exceeds its limit".into());
    }
    let envelope: ApiEnvelope<Value> =
        serde_json::from_slice(&bytes).map_err(|_| "Host handover response is invalid")?;
    if !success || !envelope.success {
        return Err(envelope
            .error
            .unwrap_or_else(|| "Host handover was denied".into()));
    }
    envelope
        .data
        .ok_or_else(|| "Host handover returned no result".into())
}

#[tauri::command]
pub(crate) async fn handover_previous_development_host(
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
) -> Result<Option<Value>, String> {
    validate_launcher_main_window(&window, "development Host handover")?;
    if !cfg!(target_os = "macos") || !config.is_dev_workspace() {
        return Err("This handover is available for macOS development Hosts".into());
    }
    let app = window.app_handle().clone();
    let bundle = tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_title("Select the previous Tobkiri Launcher Developer.app")
            .add_filter("Tobkiri Launcher", &["app"])
            .blocking_pick_file()
    })
    .await
    .map_err(|_| "Previous Host picker failed")?;
    let Some(bundle) = bundle else {
        return Ok(None);
    };
    let bundle = bundle
        .into_path()
        .map_err(|_| "Previous Host picker returned an invalid path")?;
    let app = window.app_handle().clone();
    let source = tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_title("Select the previous developer Host user_data folder")
            .blocking_pick_folder()
    })
    .await
    .map_err(|_| "Previous data picker failed")?;
    let Some(source) = source else {
        return Ok(None);
    };
    let source = source
        .into_path()
        .map_err(|_| "Previous data picker returned an invalid path")?;
    let config = config.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let secret = load_or_create_panel_bootstrap_secret(&config).map_err(|_| "Host handover authentication is unavailable")?;
        let client = reqwest::blocking::Client::builder().no_proxy().timeout(Duration::from_secs(300)).redirect(reqwest::redirect::Policy::none()).build().map_err(|_| "Host handover client is unavailable")?;
        let base = format!("http://127.0.0.1:{}/api/internal/native-host-handover", config.kernel_port);
        let response = client.post(format!("{base}/prepare")).header("X-Rumi-Desktop-Bootstrap", &secret).json(&json!({"source_root": source, "source_executable": bundle.join("Contents/MacOS/tobkiri-launcher")})).send().map_err(|_| "Host handover review is unavailable")?;
        let review = read_result(response)?;
        let plan = review.get("plan").ok_or("Host handover review is incomplete")?;
        let digest = plan.get("plan_digest").and_then(Value::as_str).ok_or("Host handover plan identity is missing")?;
        let nonce = review.get("ceremony_nonce").and_then(Value::as_str).ok_or("Host handover review identity is missing")?;
        let detail = serde_json::to_string_pretty(plan).map_err(|_| "Host handover plan is invalid")?;
        let confirmed = window.dialog().message(format!("Move the displayed verified base VM storage and import the displayed Profile, connection, model and text-history data into this updated development Host?\n\nThe previous Host and its children must have exited. Connections remain disabled until you register credentials again. Review and activate the new Profile separately. Old authority, keys and unimported data remain in the previous namespace. Interrupted migration requires exact recovery.\n\n{detail}")).title("Review Tobkiri development Host handover").kind(MessageDialogKind::Warning).buttons(MessageDialogButtons::OkCancelCustom("Move storage and import data".into(), "Cancel".into())).blocking_show();
        if !confirmed { return Ok(None) }
        // The renderer cannot supply either filesystem paths or this proof.
        // A native dialog response is the only path to the private commit.
        validate_launcher_main_window(&window, "development Host handover")?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "Host handover clock is unavailable")?.as_secs();
        let proof = sign_consent(&secret, "commit", digest, nonce, now)?;
        let response = client.post(format!("{base}/commit")).header("X-Rumi-Desktop-Bootstrap", &secret).json(&json!({"plan_digest": digest, "ceremony_nonce": nonce, "native_consent": proof})).send().map_err(|_| "Host handover status is uncertain; review recovery before any further action")?;
        read_result(response).map(Some)
    }).await.map_err(|_| "Host handover task failed".to_string())?
}

#[tauri::command]
pub(crate) async fn recover_development_host_handover(
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
) -> Result<Option<Value>, String> {
    validate_launcher_main_window(&window, "development Host recovery")?;
    if !cfg!(target_os = "macos") || !config.is_dev_workspace() {
        return Err("Development Host recovery is unavailable".into());
    }
    let config = config.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let secret = load_or_create_panel_bootstrap_secret(&config).map_err(|_| "Host recovery authentication is unavailable")?;
        let client = reqwest::blocking::Client::builder().no_proxy().timeout(Duration::from_secs(300)).redirect(reqwest::redirect::Policy::none()).build().map_err(|_| "Host recovery client is unavailable")?;
        let base = format!("http://127.0.0.1:{}/api/internal/native-host-handover", config.kernel_port);
        let response = client.post(format!("{base}/recovery-preview")).header("X-Rumi-Desktop-Bootstrap", &secret).json(&json!({})).send().map_err(|_| "Host recovery review is unavailable")?;
        let review = read_result(response)?;
        if review.get("completed").and_then(Value::as_bool) == Some(true) { return Ok(review.get("result").cloned()) }
        let plan = review.get("plan").ok_or("Host recovery review is incomplete")?;
        let digest = plan.get("plan_digest").and_then(Value::as_str).ok_or("Host recovery plan identity is missing")?;
        let nonce = review.get("ceremony_nonce").and_then(Value::as_str).ok_or("Host recovery review identity is missing")?;
        let detail = serde_json::to_string_pretty(plan).map_err(|_| "Host recovery plan is invalid")?;
        let orphaned = plan.get("stage").and_then(Value::as_str) == Some("orphaned-intent");
        let message = if orphaned {
            format!("This review was interrupted before data staging or VM storage moved. Record this exact review as abandoned? All keys and data are retained. This development Host remains blocked.\n\n{detail}")
        } else {
            format!("Restore the exact displayed base VM storage to its previous Host? New imported data is retained in staging. The updated destination remains fenced. Both the previous Host and all guest domains must be stopped.\n\n{detail}")
        };
        let accept = if orphaned { "Record abandoned review" } else { "Restore previous storage" };
        if !window.dialog().message(message).title("Review Tobkiri Host recovery").kind(MessageDialogKind::Warning).buttons(MessageDialogButtons::OkCancelCustom(accept.into(), "Cancel".into())).blocking_show() { return Ok(None) }
        validate_launcher_main_window(&window, "development Host recovery")?;
        let now = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "Host recovery clock is unavailable")?.as_secs();
        let proof = sign_consent(&secret, "recover", digest, nonce, now)?;
        let response = client.post(format!("{base}/recover")).header("X-Rumi-Desktop-Bootstrap", &secret).json(&json!({"plan_digest": digest, "ceremony_nonce": nonce, "native_consent": proof})).send().map_err(|_| "Host recovery status is uncertain; re-read recovery status")?;
        read_result(response).map(Some)
    }).await.map_err(|_| "Host recovery task failed".to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_consent_binds_exact_plan_nonce_action_window_and_expiry() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let first = sign_consent("test-secret", "commit", &digest, &"b".repeat(43), 100).unwrap();
        let second = sign_consent("test-secret", "commit", &digest, &"c".repeat(43), 100).unwrap();
        assert_ne!(first["signature"], second["signature"]);
        assert_eq!(first["expires_at"], 280);
        assert_eq!(first["window_label"], "main");
        assert_eq!(first["action"], "commit");
        assert!(sign_consent("", "commit", &digest, &"b".repeat(43), 100).is_err());
        assert!(sign_consent("test-secret", "commit", &digest, "injected\nidentity", 100).is_err());
        assert_ne!(
            first["signature"],
            sign_consent("test-secret", "recover", &digest, &"b".repeat(43), 100).unwrap()
                ["signature"]
        );
    }
}
