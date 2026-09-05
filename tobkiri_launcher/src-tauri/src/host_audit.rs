use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

use anyhow::{Context, Result};
use serde::Serialize;
use serde_json::{Map, Value};

#[derive(Debug, Clone, Serialize)]
pub struct HostAuditEntry {
    pub audit_id: String,
    pub ts: u64,
    pub function_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pack_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    pub allowed: bool,
    pub result_ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub approval_token_present: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub approval_result: Option<String>,
    pub args_summary: Value,
}

pub fn now_epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub fn write_audit_log(path: &Path, entry: &HostAuditEntry) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| {
            format!(
                "failed to create host broker audit dir at {}",
                parent.display()
            )
        })?;
    }
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(path)
        .with_context(|| format!("failed to open host broker audit log at {}", path.display()))?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).with_context(|| {
        format!(
            "failed to restrict host broker audit log at {}",
            path.display()
        )
    })?;
    let line =
        serde_json::to_string(entry).context("failed to serialize host broker audit entry")?;
    writeln!(file, "{line}").context("failed to append host broker audit log")?;
    file.sync_all()
        .context("failed to fsync host broker audit log")?;
    Ok(())
}

pub fn summarize_args(value: &Value) -> Value {
    summarize_value(value, 0, false)
}

fn summarize_value(value: &Value, depth: usize, target_binding: bool) -> Value {
    if depth >= 3 {
        return Value::String("[redacted-depth]".to_string());
    }
    match value {
        Value::Object(map) => {
            let mut summarized = Map::new();
            for (key, entry) in map {
                let normalized = key.to_ascii_lowercase();
                if should_redact_key(&normalized)
                    || (target_binding && is_target_geometry_key(&normalized))
                {
                    summarized.insert(key.clone(), Value::String("[redacted]".to_string()));
                    continue;
                }
                summarized.insert(
                    key.clone(),
                    summarize_value(
                        entry,
                        depth + 1,
                        target_binding || is_target_binding_key(&normalized),
                    ),
                );
            }
            Value::Object(summarized)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .take(20)
                .map(|item| summarize_value(item, depth + 1, target_binding))
                .collect(),
        ),
        Value::String(text) => {
            if looks_sensitive_text(text) {
                Value::String("[redacted]".to_string())
            } else if text.len() > 160 {
                Value::String(format!("{}...", &text[..160]))
            } else {
                Value::String(text.clone())
            }
        }
        other => other.clone(),
    }
}

fn is_target_binding_key(key: &str) -> bool {
    matches!(
        key,
        "window" | "target" | "target_window" | "selected_window" | "active_window"
    )
}

fn is_target_geometry_key(key: &str) -> bool {
    matches!(key, "x" | "y" | "width" | "height")
}

fn should_redact_key(key: &str) -> bool {
    matches!(
        key,
        "approval_token"
            | "token"
            | "authorization"
            | "cookie"
            | "cookies"
            | "clipboard"
            | "content"
            | "text"
            | "value"
            | "intent"
            | "key"
            | "key_combo"
            | "url"
            | "current_url"
            | "final_url"
            | "path"
            | "artifact_root"
            | "chat_store_path"
            | "title"
            | "window_title"
            | "pid"
            | "target_pid"
            | "window_id"
            | "hwnd"
            | "app"
            | "application"
            | "target_app"
            | "bundle_id"
            | "data_url"
            | "base64"
            | "file_contents"
    ) || key.ends_with("_token")
        || key.ends_with("_url")
        || key.ends_with("_path")
        || key.ends_with("_pid")
        || key.ends_with("_window_id")
        || key.ends_with("_title")
}

fn looks_sensitive_text(value: &str) -> bool {
    let trimmed = value.trim();
    trimmed.starts_with("data:") || trimmed.starts_with("-----BEGIN") || trimmed.len() > 400
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn summarize_args_redacts_computer_content_and_target_identifiers() {
        let summary = summarize_args(&json!({
            "action": "computer.type",
            "text": "private typed text",
            "url": "https://example.invalid/?secret=query",
            "window_title": "Private tab",
            "pid": 123,
            "window_id": 456,
            "target_app": "ChatGPT Atlas",
            "bundle_id": "com.openai.atlas",
            "screenshot_path": "/private/path.png",
            "diagnostics": {"completion_verified": false, "failure_stage": "verify"},
        }));
        let serialized = serde_json::to_string(&summary).unwrap();
        assert!(serialized.contains("computer.type"));
        assert!(serialized.contains("completion_verified"));
        assert!(serialized.contains("failure_stage"));
        for secret in [
            "private typed text",
            "secret=query",
            "Private tab",
            "123",
            "456",
            "ChatGPT Atlas",
            "com.openai.atlas",
            "/private/path.png",
        ] {
            assert!(!serialized.contains(secret), "leaked {secret}");
        }
    }

    #[test]
    fn summarize_screenshot_binding_redacts_nested_geometry_but_keeps_click_coordinates() {
        let summary = summarize_args(&json!({
            "action": "computer.screenshot",
            "x": 17,
            "y": 29,
            "window": {
                "app": "CANARY_PRIVATE_APP",
                "pid": 91234,
                "window_id": 56789,
                "x": 101,
                "y": 202,
                "width": 1303,
                "height": 704,
                "title": "CANARY_PRIVATE_TITLE",
                "path": "/CANARY/private.png",
                "token": "CANARY_PRIVATE_TOKEN"
            }
        }));
        assert_eq!(summary.pointer("/x"), Some(&json!(17)));
        assert_eq!(summary.pointer("/y"), Some(&json!(29)));
        for key in [
            "app",
            "pid",
            "window_id",
            "x",
            "y",
            "width",
            "height",
            "title",
            "path",
            "token",
        ] {
            assert_eq!(
                summary.pointer(&format!("/window/{key}")),
                Some(&json!("[redacted]"))
            );
        }
        let serialized = serde_json::to_string(&summary).unwrap();
        for secret in [
            "CANARY_PRIVATE_APP",
            "91234",
            "56789",
            "101",
            "202",
            "1303",
            "704",
            "CANARY_PRIVATE_TITLE",
            "/CANARY/private.png",
            "CANARY_PRIVATE_TOKEN",
        ] {
            assert!(!serialized.contains(secret), "leaked {secret}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn audit_file_is_owner_only_even_when_an_existing_mode_is_permissive() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "rumi-host-audit-mode-{}-{unique}",
            std::process::id()
        ));
        let path = directory.join("audit.jsonl");
        fs::create_dir_all(&directory).unwrap();
        fs::write(&path, "").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        write_audit_log(
            &path,
            &HostAuditEntry {
                audit_id: "host-audit-test".to_string(),
                ts: 1,
                function_id: "computer.type".to_string(),
                profile_id: None,
                pack_id: None,
                conversation_id: None,
                allowed: true,
                result_ok: false,
                approval_token_present: Some(true),
                approval_result: Some("consumed".to_string()),
                args_summary: json!({"text": "[redacted]"}),
            },
        )
        .unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(directory).ok();
    }
}
