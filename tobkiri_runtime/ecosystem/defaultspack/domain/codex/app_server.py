from __future__ import annotations

import json
import os
import ipaddress
import select
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TRANSPORTS = {"off", "stdio", "unix", "websocket_loopback", "websocket_remote"}
_APP_SERVER_WS_TOKEN_KEY = "RUMICODEX_APP_SERVER_WS_TOKEN"
_APP_SERVER_SHARED_SECRET_KEY = "RUMICODEX_APP_SERVER_SHARED_SECRET"
_APP_SERVER_SECRET_MATERIAL_TYPE = "app_server_secret"
_APP_SERVER_SAFE_CONFIG_ARGS = [
    "-c",
    'approval_policy="untrusted"',
    "-c",
    'sandbox_mode="read-only"',
]
_DEFAULT_CONNECTION_ID = "default"
_APP_SERVER_AUTH_ENV = (
    (
        "ws_token",
        "RUMI_CODEX_APP_SERVER_WS_TOKEN",
        "RUMI_CODEX_APP_SERVER_WS_TOKEN_FILE",
        "ws_token_file",
        _APP_SERVER_WS_TOKEN_KEY,
    ),
    (
        "shared_secret",
        "RUMI_CODEX_APP_SERVER_SHARED_SECRET",
        "RUMI_CODEX_APP_SERVER_SHARED_SECRET_FILE",
        "shared_secret_file",
        _APP_SERVER_SHARED_SECRET_KEY,
    ),
)
_DEFAULT_CONFIG = {
    "transport": "off",
    "enabled": False,
    "base_url": "",
    "websocket_url": "",
    "unix_socket_path": "",
    "ws_token_file": "",
    "shared_secret_file": "",
    "tool_source_enabled": False,
    "automation_endpoint_enabled": False,
    "url_secret_rejected": False,
    "account": {},
}


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_path(pack_root: Path | None = None) -> Path:
    return (pack_root or _pack_root()) / "user_data" / "settings" / "codex_app_server.json"


def _secrets_dir(pack_root: Path | None = None) -> Path:
    if pack_root is None:
        configured_override = os.getenv("RUMI_DEFAULTSPACK_SECRETS_DIR", "").strip()
        if configured_override:
            return Path(configured_override).expanduser()
        configured_user_data = os.getenv("RUMI_USER_DATA", "").strip()
        if configured_user_data:
            return Path(configured_user_data).expanduser() / "secrets"
    return (pack_root or _pack_root()) / "user_data" / "secrets"


def _get_store(pack_root: Path | None = None):
    from core_runtime.secrets_store import SecretsStore

    return SecretsStore(str(_secrets_dir(pack_root)))


def _read_config(pack_root: Path | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(_config_path(pack_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_CONFIG)
    if not isinstance(payload, dict):
        return dict(_DEFAULT_CONFIG)
    return _normalize_config(payload)


def _write_config(payload: dict[str, Any], pack_root: Path | None = None) -> None:
    path = _config_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _normalize_url(value: Any, *, allowed_schemes: set[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc:
        return ""
    return text


def _websocket_listen_url(config: dict[str, Any]) -> str:
    websocket_url = str(config.get("websocket_url") or "").strip()
    if websocket_url:
        return websocket_url
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return ""
    parsed = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme.lower(), "")
    if not scheme or not parsed.netloc:
        return ""
    path = parsed.path or ""
    return f"{scheme}://{parsed.netloc}{path}"


def _url_has_query(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        return bool(urlsplit(text).query)
    except ValueError:
        return False


def _hostname_is_loopback(hostname: str) -> bool:
    host = str(hostname or "").strip().strip("[]").lower()
    if host in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _url_is_loopback(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    return _hostname_is_loopback(parsed.hostname or "")


def _transport_url_mismatch_reason(config: dict[str, Any]) -> str:
    transport = str(config.get("transport") or "off")
    if transport not in {"websocket_loopback", "websocket_remote"}:
        return ""

    urls = [
        str(config.get("base_url") or "").strip(),
        str(config.get("websocket_url") or "").strip(),
    ]
    configured_urls = [url for url in urls if url]
    if not configured_urls:
        return ""

    has_loopback_url = any(_url_is_loopback(url) for url in configured_urls)
    has_remote_url = any(not _url_is_loopback(url) for url in configured_urls)
    if transport == "websocket_loopback" and has_remote_url:
        return "websocket_loopback transport only accepts loopback base_url/websocket_url values."
    if transport == "websocket_remote" and has_loopback_url:
        return "websocket_remote transport only accepts non-loopback base_url/websocket_url values."
    return ""


def _normalize_path(value: Any) -> str:
    return str(value or "").strip()


def _codex_auth_method_for_account_type(account_type: str) -> str:
    if account_type == "chatgpt":
        return "chatgpt_account"
    if account_type == "apiKey":
        return "platform_api_key"
    if account_type == "amazonBedrock":
        return "amazon_bedrock"
    return account_type or "unknown"


def _codex_auth_method_label(auth_method: str) -> str:
    return {
        "chatgpt_account": "ChatGPT account",
        "platform_api_key": "OpenAI API key",
        "amazon_bedrock": "Amazon Bedrock",
    }.get(auth_method, auth_method or "account")


def _codex_app_server_auth_methods(*, account_configured: bool = False, app_server_auth_configured: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "id": "chatgpt_account",
            "label": "ChatGPT account via Codex App Server",
            "credential_kind": "chatgpt_account",
            "configured": account_configured,
            "secret_material": False,
        },
        {
            "id": "app_server_secret",
            "label": "Codex App Server secret",
            "credential_kind": "codex_app_server_secret",
            "configured": app_server_auth_configured,
            "secret_material": True,
        },
    ]


def _safe_account_metadata(value: Any) -> dict[str, Any]:
    account = value if isinstance(value, dict) else {}
    account_type = str(account.get("type") or account.get("account_type") or "").strip()
    if not account_type:
        return {}
    email = str(account.get("email") or "").strip()
    plan_type = str(account.get("planType") or account.get("plan_type") or "").strip()
    requires_openai_auth = account.get("requiresOpenaiAuth")
    if requires_openai_auth is None:
        requires_openai_auth = account.get("requires_openai_auth")
    auth_method = str(account.get("auth_method") or _codex_auth_method_for_account_type(account_type)).strip()
    label = str(account.get("account_label") or "").strip()
    if not label:
        if auth_method == "chatgpt_account":
            label = email or "ChatGPT account"
        elif auth_method == "platform_api_key":
            label = "OpenAI API key"
        elif auth_method == "amazon_bedrock":
            label = "Amazon Bedrock"
        else:
            label = account_type
    result: dict[str, Any] = {
        "provider_id": "codex",
        "provider_kind": "codex",
        "type": account_type,
        "auth_method": auth_method,
        "auth_method_label": _codex_auth_method_label(auth_method),
        "account_label": label,
    }
    if email:
        result["email"] = email
    if plan_type:
        result["plan_type"] = plan_type
    if isinstance(requires_openai_auth, bool):
        result["requires_openai_auth"] = requires_openai_auth
    return result


def _infer_transport(payload: dict[str, Any], *, base_url: str, websocket_url: str, unix_socket_path: str) -> str:
    explicit = str(payload.get("transport") or payload.get("mode") or "").strip().lower()
    if explicit in TRANSPORTS:
        return explicit
    enabled = _bool(payload.get("enabled"))
    if not enabled:
        return "off"
    if websocket_url:
        return "websocket_loopback" if _url_is_loopback(websocket_url) else "websocket_remote"
    if base_url:
        return "websocket_loopback" if _url_is_loopback(base_url) else "websocket_remote"
    if unix_socket_path:
        return "unix"
    return "off"


def _normalize_config(payload: dict[str, Any]) -> dict[str, Any]:
    raw_base_url = payload.get("base_url") or payload.get("server_url")
    raw_websocket_url = payload.get("websocket_url")
    url_secret_rejected = _bool(payload.get("url_secret_rejected")) or _url_has_query(raw_base_url) or _url_has_query(raw_websocket_url)
    base_url = _normalize_url(
        raw_base_url,
        allowed_schemes={"http", "https"},
    ) if not url_secret_rejected else ""
    websocket_url = _normalize_url(raw_websocket_url, allowed_schemes={"ws", "wss"}) if not url_secret_rejected else ""
    unix_socket_path = _normalize_path(payload.get("unix_socket_path") or payload.get("socket_path"))
    transport = _infer_transport(
        payload,
        base_url=base_url,
        websocket_url=websocket_url,
        unix_socket_path=unix_socket_path,
    )
    enabled = False if transport == "off" else _bool(payload.get("enabled"))
    return {
        "transport": transport,
        "enabled": enabled,
        "base_url": base_url,
        "websocket_url": websocket_url,
        "unix_socket_path": unix_socket_path,
        "ws_token_file": _normalize_path(payload.get("ws_token_file") or payload.get("auth_token_file")),
        "shared_secret_file": _normalize_path(payload.get("shared_secret_file")),
        "tool_source_enabled": _bool(payload.get("tool_source_enabled")),
        "automation_endpoint_enabled": _bool(payload.get("automation_endpoint_enabled")),
        "url_secret_rejected": url_secret_rejected,
        "account": _safe_account_metadata(payload.get("account") or payload.get("last_account")),
    }


def _read_secret_file(path_text: str) -> str:
    path = Path(path_text).expanduser()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_stored_secret(keys: tuple[str, ...], *, pack_root: Path | None = None) -> tuple[str, str]:
    for key in keys:
        try:
            value = str(
                _get_store(pack_root)._internal_read_value(
                    key,
                    caller_id="defaultspack.codex:app_server_auth",
                )
                or ""
            ).strip()
        except Exception:
            value = ""
        if value:
            return value, key
    return "", ""


def _read_connection_app_server_auth(*, pack_root: Path | None = None) -> dict[str, str]:
    from domain.connections.store import connection_credential_ref, read_connection_credential

    payload = read_connection_credential(
        "codex",
        _APP_SERVER_SECRET_MATERIAL_TYPE,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    credential_ref = connection_credential_ref(
        "codex",
        _APP_SERVER_SECRET_MATERIAL_TYPE,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )
    ws_token = str(credentials.get("ws_token") or credentials.get("access_token") or "").strip()
    if ws_token:
        return {
            "kind": "ws_token",
            "source": "secret_store",
            "file_path": "",
            "secret_key": str(credential_ref.get("credential_id") or ""),
            "value": ws_token,
        }
    shared_secret = str(
        credentials.get("shared_secret")
        or credentials.get("app_server_secret")
        or credentials.get("secret")
        or ""
    ).strip()
    if shared_secret:
        return {
            "kind": "shared_secret",
            "source": "secret_store",
            "file_path": "",
            "secret_key": str(credential_ref.get("credential_id") or ""),
            "value": shared_secret,
        }
    return {"kind": "", "source": "missing", "file_path": "", "value": ""}


def _app_server_credential_ref(*, pack_root: Path | None = None) -> dict[str, str]:
    from domain.connections.store import connection_credential_ref

    return connection_credential_ref(
        "codex",
        _APP_SERVER_SECRET_MATERIAL_TYPE,
        connection_id=_DEFAULT_CONNECTION_ID,
        pack_root=pack_root,
    )


def _codex_app_server_auth(config: dict[str, Any], *, pack_root: Path | None = None) -> dict[str, str]:
    from core_runtime.host_contract import host_contract_value

    imported = _read_connection_app_server_auth(pack_root=pack_root)
    if imported.get("value"):
        return imported
    for kind, _env_key, _file_env_key, config_file_key, _secret_key in _APP_SERVER_AUTH_ENV:
        file_path = str(config.get(config_file_key) or "").strip()
        if file_path:
            value = _read_secret_file(file_path)
            if value:
                return {"kind": kind, "source": "file", "file_path": file_path, "value": value}
    for kind, _env_key, _file_env_key, _config_file_key, _secret_key in _APP_SERVER_AUTH_ENV:
        value = host_contract_value(f"codex_{kind}", provider_id="codex")
        if value:
            return {"kind": kind, "source": "host_contract", "file_path": "", "value": value}
    for kind, _env_key, _file_env_key, _config_file_key, secret_key in _APP_SERVER_AUTH_ENV:
        value, key = _read_stored_secret((secret_key,), pack_root=pack_root)
        if value:
            return {"kind": kind, "source": "secret_store", "file_path": "", "secret_key": key, "value": value}
    return {"kind": "", "source": "missing", "file_path": "", "value": ""}


def _endpoint_requires_auth(config: dict[str, Any]) -> bool:
    transport = str(config.get("transport") or "off")
    if transport == "websocket_remote":
        return True
    base_url = str(config.get("base_url") or "").strip()
    websocket_url = str(config.get("websocket_url") or "").strip()
    return bool(
        (base_url and not _url_is_loopback(base_url))
        or (websocket_url and not _url_is_loopback(websocket_url))
    )


def _config_is_loopback(config: dict[str, Any]) -> bool:
    base_url = str(config.get("base_url") or "")
    websocket_url = str(config.get("websocket_url") or "")
    return bool((not base_url or _url_is_loopback(base_url)) and (not websocket_url or _url_is_loopback(websocket_url)))


def _config_is_configured(config: dict[str, Any]) -> bool:
    if not config.get("enabled") or config.get("transport") == "off":
        return False
    if config.get("url_secret_rejected"):
        return False
    if _transport_url_mismatch_reason(config):
        return False
    transport = str(config.get("transport") or "off")
    if transport == "stdio":
        return True
    if transport == "unix":
        return bool(config.get("unix_socket_path"))
    if transport in {"websocket_loopback", "websocket_remote"}:
        return bool(config.get("base_url") or config.get("websocket_url"))
    return False


def codex_app_server_auth_headers(
    config: dict[str, Any] | None = None,
    *,
    pack_root: Path | None = None,
) -> dict[str, str]:
    normalized = _normalize_config(config) if isinstance(config, dict) else _read_config(pack_root)
    auth = _codex_app_server_auth(normalized, pack_root=pack_root)
    value = str(auth.get("value") or "")
    if not value:
        return {}
    if auth.get("kind") == "shared_secret":
        return {"X-Rumi-Codex-App-Server-Secret": value}
    return {"Authorization": f"Bearer {value}"}


def build_codex_app_server_command(config: dict[str, Any]) -> list[str]:
    normalized = _normalize_config(config if isinstance(config, dict) else {})
    if not normalized.get("enabled") or normalized.get("transport") == "off":
        return []
    if normalized.get("url_secret_rejected"):
        return []
    if _transport_url_mismatch_reason(normalized):
        return []
    transport = str(normalized.get("transport") or "off")
    if transport == "stdio":
        command = ["codex", "app-server", *_APP_SERVER_SAFE_CONFIG_ARGS, "--listen", "stdio://"]
    elif transport == "unix":
        unix_socket_path = str(normalized.get("unix_socket_path") or "").strip()
        command = [
            "codex",
            "app-server",
            *_APP_SERVER_SAFE_CONFIG_ARGS,
            "--listen",
            f"unix://{unix_socket_path}" if unix_socket_path else "unix://",
        ]
    elif transport == "websocket_loopback":
        listen_url = _websocket_listen_url(normalized)
        if not listen_url:
            return []
        command = ["codex", "app-server", *_APP_SERVER_SAFE_CONFIG_ARGS, "--listen", listen_url]
    else:
        return []
    if normalized.get("ws_token_file"):
        command.extend(["--ws-auth", "capability-token", "--ws-token-file", str(normalized["ws_token_file"])])
    elif normalized.get("shared_secret_file"):
        command.extend(
            [
                "--ws-auth",
                "signed-bearer-token",
                "--ws-shared-secret-file",
                str(normalized["shared_secret_file"]),
            ]
        )
    return command


def save_codex_app_server_config(payload: dict[str, Any], *, pack_root: Path | None = None) -> dict[str, Any]:
    config = _normalize_config(payload if isinstance(payload, dict) else {})
    _write_config(config, pack_root)
    return {
        "success": True,
        "provider_id": "codex",
        "app_server": codex_app_server_status(pack_root=pack_root),
    }


def clear_codex_app_server_config(*, pack_root: Path | None = None) -> dict[str, Any]:
    try:
        _config_path(pack_root).unlink()
    except OSError:
        pass
    return {
        "success": True,
        "provider_id": "codex",
        "app_server": codex_app_server_status(pack_root=pack_root),
    }


def _cache_codex_app_server_account(account: dict[str, Any], *, pack_root: Path | None = None) -> None:
    safe_account = _safe_account_metadata(account)
    if not safe_account:
        return
    config = _read_config(pack_root)
    config["account"] = safe_account
    _write_config(config, pack_root)


def codex_app_server_status(*, pack_root: Path | None = None) -> dict[str, Any]:
    config = _read_config(pack_root)
    base_url = str(config.get("base_url") or "")
    websocket_url = str(config.get("websocket_url") or "")
    account = _safe_account_metadata(config.get("account"))
    configured = _config_is_configured(config)
    auth_required = _endpoint_requires_auth(config)
    auth = _codex_app_server_auth(config, pack_root=pack_root)
    auth_configured = bool(auth.get("value"))
    url_secret_rejected = bool(config.get("url_secret_rejected"))
    transport_url_mismatch_reason = _transport_url_mismatch_reason(config)
    auth_blocked_reason = (
        "Configure a Codex App Server WS token or shared secret before using a non-loopback endpoint."
        if auth_required and not auth_configured
        else ""
    )
    url_secret_blocked_reason = (
        "Codex App Server base_url/websocket_url cannot contain query strings."
        if url_secret_rejected
        else ""
    )
    blocked_reason = url_secret_blocked_reason or transport_url_mismatch_reason or auth_blocked_reason
    if url_secret_rejected:
        connection_status = "url_secret_rejected"
        status_label = "URL secret rejected"
    elif transport_url_mismatch_reason:
        connection_status = "transport_url_mismatch"
        status_label = "Transport mismatch"
    elif auth_blocked_reason:
        connection_status = "blocked_auth_required"
        status_label = "Auth required"
    elif configured:
        connection_status = "configured"
        status_label = "Configured"
    else:
        connection_status = "not_configured"
        status_label = "Not configured"
    return {
        "provider_id": "codex",
        "provider_kind": "codex",
        "auth_type": "codex",
        "auth_methods": _codex_app_server_auth_methods(
            account_configured=bool(account),
            app_server_auth_configured=auth_configured,
        ),
        "configured": configured,
        "enabled": bool(config.get("enabled")),
        "transport": str(config.get("transport") or "off"),
        "connection_status": connection_status,
        "status_label": status_label,
        "blocked_reason": blocked_reason,
        "url_secret_rejected": url_secret_rejected,
        "transport_url_mismatch": bool(transport_url_mismatch_reason),
        "transport_url_mismatch_reason": transport_url_mismatch_reason,
        "base_url": base_url,
        "websocket_url": websocket_url,
        "unix_socket_path": str(config.get("unix_socket_path") or ""),
        "loopback": _config_is_loopback(config),
        "auth_required": auth_required,
        "auth_configured": auth_configured,
        "auth_source": str(auth.get("source") or "missing") if auth_configured else "missing",
        "auth_kind": str(auth.get("kind") or ""),
        "credential_ref": _app_server_credential_ref(pack_root=pack_root) if auth_configured else {},
        "scopes": [],
        "capabilities": ["codex.app_server.connect"] if auth_configured else [],
        "expires_at": "",
        "status": connection_status,
        "ws_token_file": str(config.get("ws_token_file") or ""),
        "shared_secret_file": str(config.get("shared_secret_file") or ""),
        "command": build_codex_app_server_command(config),
        "account": account,
        "tool_source": {
            "enabled": bool(config.get("tool_source_enabled")),
            "status": connection_status
            if connection_status in {"blocked_auth_required", "transport_url_mismatch", "url_secret_rejected"}
            else "configured"
            if config.get("tool_source_enabled") and configured
            else "disabled",
        },
        "automation_endpoint": {
            "enabled": bool(config.get("automation_endpoint_enabled")),
            "status": connection_status
            if connection_status in {"blocked_auth_required", "transport_url_mismatch", "url_secret_rejected"}
            else "configured"
            if config.get("automation_endpoint_enabled") and configured
            else "disabled",
        },
        "probe": {"status": "not_run"},
    }


def codex_app_server_probe(*, pack_root: Path | None = None, timeout: float = 2.0) -> dict[str, Any]:
    status = codex_app_server_status(pack_root=pack_root)
    if status.get("connection_status") == "url_secret_rejected":
        return {"success": False, "provider_id": "codex", "probe": {"status": "url_secret_rejected"}}
    if status.get("connection_status") == "transport_url_mismatch":
        return {"success": False, "provider_id": "codex", "probe": {"status": "transport_url_mismatch"}}
    if status.get("connection_status") == "blocked_auth_required":
        return {"success": False, "provider_id": "codex", "probe": {"status": "blocked_auth_required"}}
    base_url = str(status.get("base_url") or "").rstrip("/")
    if not base_url:
        account_result = codex_app_server_account_status(
            command=status.get("command") if isinstance(status.get("command"), list) and status.get("command") else None,
            timeout=max(float(timeout), 3.0),
        )
        account = _safe_account_metadata(account_result.get("account"))
        if account:
            _cache_codex_app_server_account(account, pack_root=pack_root)
            account_result["app_server"] = codex_app_server_status(pack_root=pack_root)
        return account_result
    auth_headers = codex_app_server_auth_headers(pack_root=pack_root)
    last_http_error: urllib.error.HTTPError | None = None
    for endpoint in ("readyz", "healthz"):
        request = urllib.request.Request(f"{base_url}/{endpoint}", method="GET")
        for header, value in auth_headers.items():
            request.add_header(header, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return {
                    "success": True,
                    "provider_id": "codex",
                    "probe": {
                        "status": "ok" if response.status < 400 else "error",
                        "http_status": int(response.status),
                        "endpoint": f"/{endpoint}",
                    },
                }
        except urllib.error.HTTPError as exc:
            last_http_error = exc
            if exc.code not in {404, 405}:
                break
        except OSError:
            return {"success": False, "provider_id": "codex", "probe": {"status": "unreachable"}}
    if last_http_error is not None:
        return {
            "success": False,
            "provider_id": "codex",
            "probe": {"status": "http_error", "http_status": int(last_http_error.code)},
        }
    return {"success": False, "provider_id": "codex", "probe": {"status": "unreachable"}}


def codex_app_server_account_status(
    *,
    timeout: float = 5.0,
    command: list[str] | None = None,
) -> dict[str, Any]:
    command = command or build_codex_app_server_command({"enabled": True, "transport": "stdio"})
    if not command:
        return {"success": False, "provider_id": "codex", "probe": {"status": "stdio_not_configured"}, "account": {}}
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    sent: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []

    def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        sent.append(message)
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    deadline = time.monotonic() + max(float(timeout), 0.1)
    try:
        send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "rumi_defaultspack",
                        "title": "Tobkiri",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        while time.monotonic() < deadline:
            line = _readline_before_deadline(proc.stdout, min(deadline, time.monotonic() + 2.0))
            if not line:
                continue
            message = _parse_json_line(line)
            if not message:
                continue
            messages.append(_safe_codex_message(message))
            if message.get("id") != 0:
                continue
            if message.get("error"):
                return _account_status_result(
                    success=False,
                    command=command,
                    sent=sent,
                    messages=messages,
                    error=message.get("error"),
                    probe_status="initialize_failed",
                )
            break
        else:
            return _account_status_result(
                success=False,
                command=command,
                sent=sent,
                messages=messages,
                error="initialize_timeout",
                probe_status="timeout",
            )

        send({"method": "initialized", "params": {}})
        send({"method": "account/read", "id": 1, "params": {"refreshToken": False}})
        while time.monotonic() < deadline:
            line = _readline_before_deadline(proc.stdout, min(deadline, time.monotonic() + 2.0))
            if not line:
                continue
            message = _parse_json_line(line)
            if not message:
                continue
            messages.append(_safe_codex_message(message))
            if message.get("id") != 1:
                continue
            if message.get("error"):
                return _account_status_result(
                    success=False,
                    command=command,
                    sent=sent,
                    messages=messages,
                    error=message.get("error"),
                    probe_status="account_read_failed",
                )
            result = message.get("result") if isinstance(message.get("result"), dict) else {}
            account = _safe_account_metadata(result.get("account"))
            requires_openai_auth = bool(result.get("requiresOpenaiAuth"))
            if requires_openai_auth and "requires_openai_auth" not in account:
                account["requires_openai_auth"] = True
            return _account_status_result(
                success=bool(account),
                command=command,
                sent=sent,
                messages=messages,
                account=account,
                requires_openai_auth=requires_openai_auth,
                probe_status="ok" if account else "auth_required" if requires_openai_auth else "not_connected",
            )
        return _account_status_result(
            success=False,
            command=command,
            sent=sent,
            messages=messages,
            error="account_read_timeout",
            probe_status="timeout",
        )
    finally:
        _terminate_process(proc)


def _account_status_result(
    *,
    success: bool,
    command: list[str],
    sent: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    account: dict[str, Any] | None = None,
    requires_openai_auth: bool = False,
    error: Any = "",
    probe_status: str,
) -> dict[str, Any]:
    return {
        "success": success,
        "provider_id": "codex",
        "transport": "stdio",
        "command": list(command),
        "account": _safe_account_metadata(account),
        "requires_openai_auth": requires_openai_auth,
        "probe": {"status": probe_status, "endpoint": "stdio://account/read"},
        "sent_methods": [str(item.get("method") or "") for item in sent],
        "messages": messages,
        "error": error,
    }


def codex_app_server_stdio_smoke(
    *,
    prompt: str,
    cwd: str | None = None,
    model: str | None = None,
    timeout: float = 60.0,
    command: list[str] | None = None,
) -> dict[str, Any]:
    command = command or build_codex_app_server_command({"enabled": True, "transport": "stdio"})
    if not command:
        return {"success": False, "provider_id": "codex", "error": "stdio_not_configured"}
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    messages: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []
    final_parts: list[str] = []
    approval_requests: list[dict[str, Any]] = []
    thread_id = ""
    turn_id = ""
    completed = False

    def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        sent.append(message)
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    deadline = time.monotonic() + max(float(timeout), 0.1)
    try:
        send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "rumi_defaultspack",
                        "title": "Tobkiri",
                        "version": "0.1.0",
                    }
                },
            }
        )
        send({"method": "initialized", "params": {}})
        thread_params = _compact_dict({"model": model, "cwd": cwd})
        send({"method": "thread/start", "id": 1, "params": thread_params})

        while time.monotonic() < deadline:
            read_deadline = min(deadline, time.monotonic() + 2.0) if final_parts else deadline
            line = _readline_before_deadline(proc.stdout, read_deadline)
            if not line:
                if final_parts:
                    completed = True
                break
            message = _parse_json_line(line)
            if not message:
                continue
            messages.append(_safe_codex_message(message))
            if message.get("id") == 1:
                if message.get("error"):
                    return _stdio_smoke_result(
                        success=False,
                        command=command,
                        sent=sent,
                        messages=messages,
                        error=message.get("error"),
                    )
                thread_id = str(_nested(message, "result", "thread", "id") or "")
                if thread_id:
                    turn_params = _compact_dict(
                        {
                            "threadId": thread_id,
                            "cwd": cwd,
                            "model": model,
                            "input": [{"type": "text", "text": prompt}],
                        }
                    )
                    send({"method": "turn/start", "id": 2, "params": turn_params})
            elif message.get("id") == 2:
                if message.get("error"):
                    return _stdio_smoke_result(
                        success=False,
                        command=command,
                        sent=sent,
                        messages=messages,
                        thread_id=thread_id,
                        error=message.get("error"),
                    )
                turn_id = str(_nested(message, "result", "turn", "id") or "")
            else:
                method = str(message.get("method") or "")
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                if method == "item/agentMessage/delta":
                    final_parts.append(_redact_known_secrets(str(params.get("delta") or "")))
                    turn_id = turn_id or str(params.get("turnId") or "")
                elif method == "item/completed" and final_parts:
                    completed = True
                    break
                elif method == "turn/started":
                    turn_id = turn_id or str(_nested(params, "turn", "id") or "")
                elif method == "turn/completed":
                    completed = True
                    turn_id = turn_id or str(_nested(params, "turn", "id") or "")
                    break
                elif "approval" in method.lower():
                    approval_requests.append(_safe_codex_message(message))
        return _stdio_smoke_result(
            success=bool(thread_id and turn_id and completed),
            command=command,
            sent=sent,
            messages=messages,
            thread_id=thread_id,
            turn_id=turn_id,
            final_output="".join(final_parts),
            approval_requests=approval_requests,
            error="" if completed else "turn_not_completed",
        )
    finally:
        _terminate_process(proc)


def _stdio_smoke_result(
    *,
    success: bool,
    command: list[str],
    sent: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    thread_id: str = "",
    turn_id: str = "",
    final_output: str = "",
    approval_requests: list[dict[str, Any]] | None = None,
    error: Any = "",
) -> dict[str, Any]:
    return {
        "success": success,
        "provider_id": "codex",
        "transport": "stdio",
        "command": list(command),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "final_output": final_output,
        "approval_required": bool(approval_requests),
        "approval_requests": approval_requests or [],
        "sent_methods": [str(item.get("method") or "") for item in sent],
        "messages": messages,
        "error": error,
    }


def _readline_before_deadline(stream: Any, deadline: float) -> str:
    if stream is None:
        return ""
    remaining = max(deadline - time.monotonic(), 0)
    if remaining <= 0:
        return ""
    try:
        readable, _writable, _errors = select.select([stream], [], [], remaining)
    except Exception:
        return str(stream.readline() or "")
    if not readable:
        return ""
    return str(stream.readline() or "")


def _parse_json_line(line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _nested(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _safe_codex_message(message: dict[str, Any]) -> dict[str, Any]:
    text = _redact_known_secrets(json.dumps(message, ensure_ascii=False))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"message": "[unserializable]"}
    return payload if isinstance(payload, dict) else {"message": payload}


def _redact_known_secrets(text: str) -> str:
    from core_runtime.host_contract import host_contract_value

    values: set[str] = set()
    for key in (
        "RUMI_CODEX_ACCESS_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "RUMI_CODEX_APP_SERVER_WS_TOKEN",
        "RUMI_CODEX_APP_SERVER_SHARED_SECRET",
    ):
        value = host_contract_value(key)
        if value:
            values.add(value)
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
