from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .browser_companion_bridge import (
    BrowserCompanionBridgeStore,
    candidate_base_urls,
)


_DATA_URL_RE = re.compile(r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>.+)$", re.IGNORECASE)
_PAGE_ACTIONS_REQUIRING_APPROVAL = {
    "page.navigate",
    "page.snapshot",
    "page.capture",
    "page.extract",
    "page.click",
    "page.type",
    "page.press",
    "page.scroll",
}
_READ_ONLY_PAGE_ACTIONS = {"page.snapshot", "page.capture", "page.extract"}


class BrowserCompanionController:
    """Cookie-bearing browser extension bridge for DOM-aware browser control."""

    def __init__(
        self,
        *,
        artifact_root: Path | None = None,
        bridge_store: BrowserCompanionBridgeStore | None = None,
    ) -> None:
        pack_root = Path(__file__).resolve().parents[2]
        self._pack_root = pack_root
        self._artifact_root = artifact_root or pack_root / "user_data" / "artifacts" / "browser_companion"
        self._bridge = bridge_store or BrowserCompanionBridgeStore()
        self._approval_path = self._bridge.root_dir / "browser_companion_approvals.json"

    def run(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload or {})
        context = context if isinstance(context, dict) else {}
        normalized = self._normalize_action(action)
        if normalized in {"session", "bridge.status"}:
            return self._session(context)
        if normalized == "bridge.pairing":
            return self._pairing(context, rotate=bool(payload.get("rotate")))
        if normalized in {"browser.clients", "browser.sessions"}:
            return self._clients()
        if normalized == "browser.select_client":
            return self._select_client(payload)
        if normalized == "browser.tabs":
            return self._run_remote("browser.tabs", payload, context, timeout_seconds=10.0)
        if normalized == "browser.select_tab":
            return self._run_remote("browser.select_tab", payload, context, timeout_seconds=10.0)
        if normalized == "page.navigate":
            return self._run_remote("page.navigate", payload, context, timeout_seconds=20.0)
        if normalized == "page.snapshot":
            return self._run_remote(
                "page.snapshot",
                payload,
                context,
                timeout_seconds=20.0,
                attach_capture=bool(payload.get("include_capture")),
            )
        if normalized == "page.capture":
            return self._run_remote(
                "page.capture",
                payload,
                context,
                timeout_seconds=20.0,
                attach_capture=True,
            )
        if normalized == "page.extract":
            return self._run_remote("page.extract", payload, context, timeout_seconds=20.0)
        if normalized == "page.click":
            return self._run_remote("page.click", payload, context, timeout_seconds=20.0)
        if normalized == "page.type":
            return self._run_remote("page.type", payload, context, timeout_seconds=20.0)
        if normalized == "page.press":
            return self._run_remote("page.press", payload, context, timeout_seconds=20.0)
        if normalized == "page.scroll":
            return self._run_remote("page.scroll", payload, context, timeout_seconds=20.0)
        if normalized == "page.highlight":
            return self._run_remote("page.highlight", payload, context, timeout_seconds=20.0)
        if normalized == "page.clear_highlight":
            return self._run_remote("page.clear_highlight", payload, context, timeout_seconds=20.0)
        raise ValueError(f"Unsupported browser companion action: {action}")

    @staticmethod
    def _normalize_action(action: str) -> str:
        raw = str(action or "").strip()
        aliases = {
            "": "session",
            "pairing": "bridge.pairing",
            "clients": "browser.clients",
            "select_client": "browser.select_client",
            "tabs": "browser.tabs",
            "select_tab": "browser.select_tab",
            "navigate": "page.navigate",
            "snapshot": "page.snapshot",
            "capture": "page.capture",
            "extract": "page.extract",
            "click": "page.click",
            "type": "page.type",
            "press": "page.press",
            "scroll": "page.scroll",
            "highlight": "page.highlight",
            "clear_highlight": "page.clear_highlight",
        }
        return aliases.get(raw, raw)

    def _pairing(self, context: dict[str, Any], *, rotate: bool) -> dict[str, Any]:
        config = self._bridge.ensure_pairing(rotate=rotate)
        return {
            "action": "bridge.pairing",
            "pairing": {
                "pairing_token": config.get("pairing_token"),
                "server_urls": candidate_base_urls(context),
                "config_dir": str(self._bridge.root_dir),
                "updated_at": config.get("updated_at") or config.get("created_at"),
            },
        }

    def _session(self, context: dict[str, Any]) -> dict[str, Any]:
        clients = self._bridge.list_clients()
        active_client = None
        for client in clients:
            if client.get("is_active"):
                active_client = client
                break
        setup_state = self._setup_state(context, clients=clients)
        return {
            "action": "session",
            "pairing": self._pairing(context, rotate=False).get("pairing"),
            "clients": clients,
            "active_client_id": (
                active_client.get("client_id")
                if isinstance(active_client, dict)
                else self._bridge.active_client_id()
            ),
            "active_client": active_client,
            "setup_required": setup_state.get("status") == "missing",
            "setup_state": setup_state,
            "capabilities": {
                "multi_browser": True,
                "dom_snapshot": True,
                "semantic_dom": True,
                "accessible_labels": True,
                "user_session_cookies": True,
                "browser_tab_capture": True,
                "browser_profile_metadata": True,
                "semantic_targeting": ["element_id", "selector", "text", "text_query", "accessible_name", "role", "semantic_id", "nearby_text"],
                "element_actions": ["click", "type", "press", "scroll", "extract", "highlight", "clear_highlight"],
            },
        }

    def _clients(self) -> dict[str, Any]:
        return {
            "action": "browser.clients",
            "clients": self._bridge.list_clients(),
            "active_client_id": self._bridge.active_client_id(),
        }

    def _select_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._bridge.resolve_client(
            client_id=str(payload.get("client_id") or ""),
            browser_profile_id=str(payload.get("browser_profile_id") or ""),
            installation_id=str(payload.get("installation_id") or ""),
            browser=str(payload.get("browser") or payload.get("browser_name") or ""),
            label=str(payload.get("label") or ""),
            profile_label=str(payload.get("profile_label") or payload.get("profileLabel") or ""),
        )
        if client is None:
            return {
                "action": "browser.select_client",
                "is_error": True,
                "reason": "No connected browser companion client matched the request.",
                "clients": self._bridge.list_clients(include_stale=True),
            }
        self._bridge.set_active_client(str(client.get("client_id") or ""))
        client = self._bridge.get_client(str(client.get("client_id") or "")) or client
        return {
            "action": "browser.select_client",
            "client": client,
            "active_client_id": client.get("client_id"),
        }

    def _resolve_target_client(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        explicit = self._bridge.resolve_client(
            client_id=str(payload.get("client_id") or ""),
            browser_profile_id=str(payload.get("browser_profile_id") or ""),
            installation_id=str(payload.get("installation_id") or ""),
            browser=str(payload.get("browser") or payload.get("browser_name") or ""),
            label=str(payload.get("label") or ""),
            profile_label=str(payload.get("profile_label") or payload.get("profileLabel") or ""),
        )
        if explicit is not None:
            return explicit
        active_client_id = self._bridge.active_client_id()
        if active_client_id:
            active = self._bridge.get_client(active_client_id)
            if active is not None:
                return active
        clients = self._bridge.list_clients()
        return clients[0] if clients else None

    def _run_remote(
        self,
        remote_action: str,
        payload: dict[str, Any],
        context: dict[str, Any],
        *,
        timeout_seconds: float,
        attach_capture: bool = False,
    ) -> dict[str, Any]:
        client = self._resolve_target_client(payload)
        if client is None:
            return {
                "action": remote_action,
                "is_error": True,
                "error_code": "BROWSER_COMPANION_CLIENT_MISSING",
                "reason": "No connected browser companion clients are available. Pair the extension first.",
                "pairing": self._pairing(context, rotate=False).get("pairing"),
                "clients": self._bridge.list_clients(include_stale=True),
                "setup_required": True,
                "setup_state": self._setup_state(context, clients=[]),
                "retry_after_setup": True,
            }
        approval_payload = self._approval_payload(remote_action, payload, client)
        if self._read_only_blocks(remote_action, context):
            return {
                "action": remote_action,
                "is_error": True,
                "reason": "Browser companion is in read-only safety mode for this request.",
                "client": client,
                "requires_approval": False,
            }
        if (
            self._requires_approval(remote_action)
            and not self._context_allows_remote_action(context)
            and not self._consume_approval(payload, remote_action, approval_payload)
        ):
            return self._approval_required(remote_action, approval_payload, client)

        self._bridge.set_active_client(str(client.get("client_id") or ""))
        remote_payload = self._remote_payload(payload)
        if remote_action.startswith("page.") and remote_payload.get("tab_id") is None:
            active_tab_id = client.get("active_tab_id")
            if active_tab_id is not None:
                remote_payload["tab_id"] = active_tab_id
        command = self._bridge.create_command(
            str(client.get("client_id") or ""),
            {
                "action": remote_action,
                "payload": remote_payload,
            },
        )
        completed = self._bridge.wait_for_command(
            str(command.get("command_id") or ""),
            timeout_seconds=timeout_seconds,
        )
        if completed.get("status") != "completed":
            return {
                "action": remote_action,
                "is_error": True,
                "reason": "Timed out waiting for the browser companion extension to respond.",
                "client": client,
                "command_id": command.get("command_id"),
            }
        result = completed.get("result") if isinstance(completed.get("result"), dict) else {}
        client = self._bridge.get_client(str(client.get("client_id") or "")) or client
        semantics = self._action_semantics(remote_action, result)
        output = {
            "action": remote_action,
            "client": client,
            "client_id": client.get("client_id"),
            **self._client_profile_fields(client),
            "command_id": command.get("command_id"),
            **semantics,
            "result": result,
        }
        if bool(result.get("is_error")):
            output["is_error"] = True
            output["reason"] = result.get("reason") or result.get("error") or "Browser companion command failed."
        else:
            output["is_error"] = False
        if attach_capture or self._remote_result_contains_capture(result):
            artifact = self._save_capture_artifact(result, remote_action)
            if artifact is not None:
                output.update(artifact)
        if isinstance(result.get("snapshot"), dict):
            output["snapshot"] = result.get("snapshot")
        elif isinstance(result.get("snapshot"), list):
            output["snapshot"] = result.get("snapshot")
        if "snapshot_metadata" in result:
            output["snapshot_metadata"] = result.get("snapshot_metadata")
        if "tabs" in result:
            output["tabs"] = result.get("tabs")
        if "tab" in result:
            output["tab"] = result.get("tab")
        if "url" in result and not output.get("url"):
            output["url"] = result.get("url")
        if "data" in result and "data" not in output:
            output["data"] = result.get("data")
        if "elements" in result:
            output["elements"] = result.get("elements")
        elif isinstance(result.get("snapshot"), dict) and isinstance(result["snapshot"].get("nodes"), list):
            output["elements"] = result["snapshot"].get("nodes")
        return output

    def _setup_state(self, context: dict[str, Any], *, clients: list[dict[str, Any]]) -> dict[str, Any]:
        extension_root = self._browser_extension_root()
        state = {
            "status": "ok" if clients else "missing",
            "missing": [] if clients else ["browser_companion_client"],
            "reason": (
                "At least one browser companion client is paired."
                if clients
                else "No browser companion clients are paired with this defaultspack server."
            ),
            "ui": {
                "surface": "defaultspack.sidebar",
                "sidebar_item_id": "browser_companion",
                "settings_field_id": "browser_companion_setup_guide",
            },
            "extension": {
                "type": "chromium_manifest_v3",
                "path": str(extension_root),
                "manifest_path": str(extension_root / "manifest.json"),
                "options_page": "Rumi Browser Companion extension options",
            },
            "server_urls": candidate_base_urls(context),
            "tool_actions": {
                "refresh_pairing": {"tool": "browser_companion", "args": {"action": "bridge.pairing"}},
                "check_session": {"tool": "browser_companion", "args": {"action": "session"}},
            },
            "steps": [
                {
                    "id": "open_extensions",
                    "label": "Open the Chromium extensions page and enable Developer mode.",
                },
                {
                    "id": "load_unpacked",
                    "label": "Load the Rumi Browser Companion unpacked extension folder.",
                    "path": str(extension_root),
                },
                {
                    "id": "copy_pairing",
                    "label": "Use browser_companion bridge.pairing to copy a server URL and pairing token.",
                },
                {
                    "id": "poll_bridge",
                    "label": "Paste the values in the extension options page and click Poll Bridge Now.",
                },
                {
                    "id": "verify_session",
                    "label": "Run browser_companion session and confirm clients is not empty.",
                },
            ],
        }
        if clients:
            state["client_count"] = len(clients)
        return state

    def _browser_extension_root(self) -> Path:
        return self._pack_root.parent / "defaultspack" / "browser_extensions" / "rumi_browser_companion"

    @staticmethod
    def _client_profile_fields(client: dict[str, Any]) -> dict[str, Any]:
        client_profile = client.get("client_profile") if isinstance(client.get("client_profile"), dict) else {}
        browser_profile_id = client.get("browser_profile_id") or client_profile.get("browser_profile_id")
        profile_label = client.get("profile_label") or client_profile.get("profile_label")
        installation_id = client.get("installation_id") or client_profile.get("installation_id")
        return {
            "browser_profile_id": browser_profile_id,
            "profile_label": profile_label,
            "installation_id": installation_id,
            "client_profile": {
                **client_profile,
                "browser_profile_id": browser_profile_id or "",
                "profile_label": profile_label or "",
                "installation_id": installation_id or "",
            },
        }

    @staticmethod
    def _client_profile_fields(client: dict[str, Any]) -> dict[str, Any]:
        client_profile = client.get("client_profile") if isinstance(client.get("client_profile"), dict) else {}
        browser_profile_id = client.get("browser_profile_id") or client_profile.get("browser_profile_id")
        profile_label = client.get("profile_label") or client_profile.get("profile_label")
        installation_id = client.get("installation_id") or client_profile.get("installation_id")
        return {
            "browser_profile_id": browser_profile_id,
            "profile_label": profile_label,
            "installation_id": installation_id,
            "client_profile": {
                **client_profile,
                "browser_profile_id": browser_profile_id or "",
                "profile_label": profile_label or "",
                "installation_id": installation_id or "",
            },
        }

    @staticmethod
    def _requires_approval(remote_action: str) -> bool:
        return remote_action in _PAGE_ACTIONS_REQUIRING_APPROVAL

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on", "allow", "allowed"}
        return False

    @classmethod
    def _context_allows_remote_action(cls, context: dict[str, Any]) -> bool:
        if not isinstance(context, dict):
            return False
        policy = context.get("profile_policy")
        if not isinstance(policy, dict):
            policy = context.get("tool_policy")
        if not isinstance(policy, dict):
            runtime_profile = context.get("runtime_profile")
            policy = runtime_profile.get("policy") if isinstance(runtime_profile, dict) else {}
        if isinstance(policy, dict) and cls._truthy(policy.get("yolo_mode")):
            return True
        if cls._truthy(context.get("yolo_mode")):
            return True
        return bool(context.get("_tool_server_approval_token_valid") is True)

    @classmethod
    def _read_only_blocks(cls, remote_action: str, context: dict[str, Any]) -> bool:
        if remote_action not in _PAGE_ACTIONS_REQUIRING_APPROVAL or remote_action in _READ_ONLY_PAGE_ACTIONS:
            return False
        if not isinstance(context, dict):
            return False
        candidates = [context.get("browser_companion_safety"), context.get("safety")]
        policy = context.get("profile_policy")
        if isinstance(policy, dict):
            candidates.extend([policy.get("browser_companion_safety"), policy.get("safety")])
        settings = context.get("tool_settings")
        if isinstance(settings, dict):
            companion = settings.get("browser_companion")
            if isinstance(companion, dict):
                candidates.append(companion.get("safety"))
                values = companion.get("values")
                if isinstance(values, dict):
                    candidates.append(values.get("safety"))
        return any(str(value or "").strip().lower() == "read_only" for value in candidates)

    @staticmethod
    def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in dict(payload or {}).items()
            if key not in {"approved", "approval_token"}
        }

    def _approval_payload(
        self,
        remote_action: str,
        payload: dict[str, Any],
        client: dict[str, Any],
    ) -> dict[str, Any]:
        approval_payload = self._safe_payload(payload)
        approval_payload["action"] = remote_action
        client_id = str(client.get("client_id") or "")
        if client_id:
            approval_payload["client_id"] = client_id
        if remote_action.startswith("page.") and approval_payload.get("tab_id") is None:
            active_tab_id = client.get("active_tab_id")
            if active_tab_id is not None:
                approval_payload["tab_id"] = active_tab_id
        return approval_payload

    def _approval_required(
        self,
        remote_action: str,
        approval_payload: dict[str, Any],
        client: dict[str, Any],
    ) -> dict[str, Any]:
        token = self._issue_approval(remote_action, approval_payload)
        return {
            "action": remote_action,
            "client": client,
            "client_id": client.get("client_id"),
            "is_error": False,
            "requires_approval": True,
            "approval_required": True,
            "approval_token": token,
            "approval_expires_in_seconds": 300,
            "approval_hint": (
                "Repeat the same browser companion action with payload.approval_token "
                "after explicit user confirmation."
            ),
            "payload": approval_payload,
        }

    def _issue_approval(self, remote_action: str, approval_payload: dict[str, Any]) -> str:
        approvals = self._read_approvals()
        token = secrets.token_urlsafe(24)
        approvals[token] = {
            "action": remote_action,
            "payload": approval_payload,
            "expires_at": time.time() + 300,
        }
        self._write_approvals(approvals)
        return token

    def _consume_approval(
        self,
        payload: dict[str, Any],
        remote_action: str,
        expected_payload: dict[str, Any],
    ) -> bool:
        token = str((payload or {}).get("approval_token") or "").strip()
        if not token:
            return False
        approvals = self._read_approvals()
        record = approvals.pop(token, None)
        self._write_approvals(approvals)
        if not isinstance(record, dict):
            return False
        if record.get("action") != remote_action:
            return False
        if record.get("payload") != expected_payload:
            return False
        if float(record.get("expires_at") or 0) < time.time():
            return False
        return True

    def _read_approvals(self) -> dict[str, Any]:
        try:
            value = json.loads(self._approval_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _write_approvals(self, approvals: dict[str, Any]) -> None:
        self._approval_path.parent.mkdir(parents=True, exist_ok=True)
        self._approval_path.write_text(
            json.dumps(approvals, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _action_semantics(remote_action: str, result: dict[str, Any]) -> dict[str, bool]:
        requires_foreground = result.get("requires_foreground")
        can_parallel = result.get("can_parallel_user_work")
        if isinstance(requires_foreground, bool) and isinstance(can_parallel, bool):
            return {
                "requires_foreground": requires_foreground,
                "can_parallel_user_work": can_parallel,
            }
        capture = result.get("capture") if isinstance(result.get("capture"), dict) else {}
        if remote_action == "page.capture" or isinstance(capture.get("data_url"), str):
            return {
                "requires_foreground": True,
                "can_parallel_user_work": False,
            }
        if remote_action == "browser.select_tab":
            return {
                "requires_foreground": True,
                "can_parallel_user_work": False,
            }
        if remote_action in {
            "browser.tabs",
            "page.navigate",
            "page.snapshot",
            "page.click",
            "page.type",
            "page.press",
            "page.scroll",
            "page.extract",
            "page.highlight",
            "page.clear_highlight",
        }:
            return {
                "requires_foreground": False,
                "can_parallel_user_work": True,
            }
        return {}

    @staticmethod
    def _remote_payload(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "client_id",
            "browser_profile_id",
            "profile_label",
            "profileLabel",
            "installation_id",
            "browser",
            "browser_name",
            "label",
            "tab_id",
            "window_id",
            "url",
            "element_id",
            "selector",
            "selectors",
            "text",
            "text_query",
            "textQuery",
            "accessible_name",
            "accessibleName",
            "role",
            "semantic_id",
            "semanticId",
            "nearby_text",
            "nearbyText",
            "value",
            "input_text",
            "inputText",
            "name",
            "key",
            "keys",
            "code",
            "modifiers",
            "repeat",
            "direction",
            "amount",
            "x",
            "y",
            "top",
            "left",
            "delta_x",
            "delta_y",
            "behavior",
            "mode",
            "limit",
            "include_hidden",
            "include_html",
            "include_capture",
            "include_attributes",
            "attribute_names",
            "wait_for",
            "timeout_ms",
            "format",
            "quality",
            "duration_ms",
            "color",
            "label",
            "clear_existing",
            "include_semantics",
        }
        return {key: value for key, value in payload.items() if key in allowed and value is not None}

    @staticmethod
    def _remote_result_contains_capture(result: dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if isinstance(result.get("data_url"), str) and result.get("data_url"):
            return True
        capture = result.get("capture")
        return isinstance(capture, dict) and isinstance(capture.get("data_url"), str) and bool(capture.get("data_url"))

    def _save_capture_artifact(self, result: dict[str, Any], remote_action: str) -> dict[str, Any] | None:
        capture = result.get("capture") if isinstance(result.get("capture"), dict) else result
        data_url = str(capture.get("data_url") or "").strip()
        match = _DATA_URL_RE.match(data_url)
        if not match:
            return None
        try:
            raw = base64.b64decode(match.group("data"), validate=True)
        except (ValueError, binascii.Error):
            return None
        extension = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
        }.get(match.group("mime").lower(), "png")
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        path = self._artifact_root / f"{remote_action.replace('.', '-')}-{int(time.time() * 1000)}.{extension}"
        path.write_bytes(raw)
        record = {
            "path": str(path),
            "mime_type": match.group("mime"),
            "data_url": data_url,
        }
        if isinstance(capture.get("image_size"), dict):
            record["image_size"] = capture.get("image_size")
        if "target_window" in capture:
            record["target_window"] = capture.get("target_window")
        if "marker" in capture:
            record["marker"] = capture.get("marker")
        if "click_marker" in capture:
            record["click_marker"] = capture.get("click_marker")
        if "drag_marker" in capture:
            record["drag_marker"] = capture.get("drag_marker")
        return record
