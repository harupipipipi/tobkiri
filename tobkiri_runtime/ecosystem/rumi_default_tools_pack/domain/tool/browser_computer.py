from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import time
import webbrowser
import zlib
import base64
from pathlib import Path
from typing import Any
from time import monotonic as _trace_monotonic

from ..computer.trace import (
    computer_action_trace,
    emit_computer_trace,
    requested_delivery_mode,
    result_trace_facts,
    target_trace_facts,
)

_CLIPBOARD_PREVIEW_CHARS = 500
_DARWIN_AUTOMATION_TIMEOUT_SECONDS = 2
_DARWIN_CGEVENT_TIMEOUT_SECONDS = 8
_DARWIN_SCREENSHOT_TIMEOUT_SECONDS = 10
_QUARTZ_BRIDGE_MAX_ITEMS = 256
_QUARTZ_QUERY_SUCCESS_OUTCOMES = frozenset({
    "success_empty", "success_nonempty", "success_nonempty_truncated",
})
_COMPUTER_APPROVAL_PROMPT = (
    "承認が必要です。foreground/on-screen 操作も利用できます。"
    "リクエストを承認するか、foreground 作業を選んでください。"
)
_BROWSER_TEXT_INPUT_RECOMMENDED_NEXT_ACTIONS = [
    "computer.type",
    "computer.key",
    "computer.click",
    "computer.screenshot",
    "computer.observe",
]
_BROWSER_TEXT_INPUT_GUIDANCE = (
    "If the browser page or search field is ready, use computer.type for text input "
    "and computer.key for Enter or shortcuts; normal approval gates still apply. "
    "The computer.type text must be the literal user-requested URL, query, or form "
    "text to enter; do not type the current URL, app name, or window title unless "
    "that is exactly what the user asked to enter."
)
_COMPUTER_TYPE_SUCCESS_RECOMMENDED_NEXT_ACTIONS = [
    "computer.key",
    "computer.observe",
    "computer.screenshot",
]
_COMPUTER_TYPE_SUCCESS_GUIDANCE = (
    "Text input completed. If this was a browser search or address field, continue "
    "with computer.key using Enter/Return to submit, or use computer.observe or "
    "computer.screenshot to inspect the page. Do not reopen the same page just to "
    "submit typed text. For the next text entry, use the user-requested query or "
    "URL, not the current URL, app name, or window title."
)
_COMPUTER_TYPE_SUCCESS_TASK_PROGRESS = {
    "status": "text_entered",
    "location": "current_focused_field",
    "browser_search_or_address_field": "submit_pending",
}
_COMPUTER_TYPE_SUCCESS_NEXT_ACTION = {
    "preferred": {
        "action": "computer.key",
        "payload": {"key": "return"},
        "purpose": "submit the typed query or URL",
    },
    "alternatives": [
        {"action": "computer.observe", "purpose": "confirm the current target state"},
        {"action": "computer.screenshot", "purpose": "inspect the visible browser field or page"},
    ],
}
_COMPUTER_TYPE_SUCCESS_AVOID_ACTIONS = [
    {
        "action": "browser.open_url",
        "scope": "same setup page",
        "reason": "Do not reopen the page just to submit text that was already typed.",
    },
    {
        "action": "computer.type",
        "scope": "current URL, app name, or window title",
        "reason": "Only type those values when the user explicitly requested that literal text.",
    },
]
_DARWIN_BROWSER_BUNDLE_ID_ALIASES = {
    "atlas": ("com.openai.atlas",),
    "chatgpt atlas": ("com.openai.atlas",),
}
_SELECTED_WINDOW_IDENTITY_DIAGNOSTIC_CONTRACT = "rumi.mac.selected_window_identity.v1"
_SELECTED_WINDOW_IDENTITY_PRIVATE_FIELDS = (
    "_rumi_owner_alias_match",
    "_rumi_target_process_match",
    "_rumi_target_bundle_match",
)
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_SCREENSHOT_CAPTURE_DRIVERS = {
    "none",
    "mac_swift_host",
    "mac_screencapture_window",
    "mac_screencapture_rect",
    "mac_screencapture_display",
    "windows_native",
    "linux_native",
}
_SCREENSHOT_TARGET_BINDING_SOURCES = {
    "explicit_window",
    "explicit_identifiers",
    "enumerated_match",
    "persisted_selection",
    "active_window",
    "none",
}


def _key_press_count(payload: dict[str, Any]) -> int:
    for key in ("count", "times", "repeat"):
        if key not in payload:
            continue
        try:
            return max(1, min(200, int(payload.get(key))))
        except (TypeError, ValueError):
            return 1
    return 1


def _normalize_key_name(key: Any) -> str:
    value = str(key or "").strip()
    aliases = {
        "retrun": "return",
        "retun": "return",
        "newline": "return",
        "new_line": "return",
        "bksp": "backspace",
        "bs": "backspace",
        "back": "backspace",
    }
    return aliases.get(value.lower(), value)


def _running_under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _key_combo_from_payload(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("key_combo") or "").strip()
    if explicit:
        parts = [part.strip() for part in explicit.split("+") if part.strip()]
        if not parts:
            return ""
        parts[-1] = _normalize_key_name(parts[-1])
        return "+".join(parts)
    key = payload.get("key")
    if key is None:
        return ""
    modifiers = payload.get("modifiers")
    if not isinstance(modifiers, list):
        modifier = payload.get("modifier")
        modifiers = [modifier] if modifier else []
    parts = [str(item).strip() for item in modifiers if str(item or "").strip()]
    parts.append(_normalize_key_name(key))
    return "+".join(parts)


def _current_python_snippet_command(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class BrowserComputerController:
    """Generic browser/computer action controller with approval gates."""

    def __init__(self, artifact_root: Path | None = None) -> None:
        pack_root = Path(__file__).resolve().parents[2]
        self._custom_artifact_root = artifact_root is not None
        self._artifact_root = artifact_root or pack_root / "user_data" / "artifacts" / "computer"
        self._session_path = pack_root / "user_data" / "shared" / "browser_sessions.json"
        self._approval_path = pack_root / "user_data" / "shared" / "browser_computer_approvals.json"
        self._browser_root = pack_root / "user_data" / "shared" / "browser"
        self._profile_root = self._browser_root / "profiles"
        # Lazy-initialized pack service over the canonical ComputerHost boundary.
        self._computer_seat: Any = None

    def _get_computer_seat(self):
        """Lazy-create the ComputerToolService to avoid import cycles."""
        if self._computer_seat is None:
            from ..computer.factory import create_default_computer_tool_service

            self._computer_seat = create_default_computer_tool_service()
        return self._computer_seat

    def run(self, action: str, payload: dict[str, Any] | None = None, *, yolo_mode: bool = False) -> dict[str, Any]:
        """Run one action and emit content-free controller boundary facts."""
        normalized_action = self._normalize_action(action)
        normalized_payload = dict(payload or {})
        started = _trace_monotonic()
        with computer_action_trace(normalized_action):
            emit_computer_trace(
                "controller.start",
                normalized_action,
                requested_delivery_mode=requested_delivery_mode(normalized_payload),
                approval_replay=bool(yolo_mode),
                **target_trace_facts(normalized_payload),
            )
            try:
                result = self._run_action(normalized_action, normalized_payload, yolo_mode=yolo_mode)
            except Exception:
                emit_computer_trace(
                    "controller.result",
                    normalized_action,
                    duration_ms=(_trace_monotonic() - started) * 1000,
                    result_ok=False,
                    error_code="CONTROLLER_EXCEPTION",
                )
                raise
            if (
                normalized_action == "computer.key"
                and isinstance(result, dict)
                and result.get("executed") is True
                and not self._seat_key_effect_verified(result)
            ):
                self._mark_key_delivery_unverified(result)
            emit_computer_trace(
                "controller.result",
                normalized_action,
                duration_ms=(_trace_monotonic() - started) * 1000,
                **result_trace_facts(result),
            )
            return result

    def _run_action(self, action: str, payload: dict[str, Any] | None = None, *, yolo_mode: bool = False) -> dict[str, Any]:
        action = self._normalize_action(action)
        payload = payload or {}
        yolo_mode = self._truthy(yolo_mode)
        if action == "browser.open_url":
            return self._open_url(str(payload.get("url", "")), payload=payload, dry_run=self._truthy(payload.get("dry_run")), yolo_mode=yolo_mode)
        if action == "browser.session":
            return {"action": action, "platform": platform.system(), "capabilities": self._capabilities(), "session": self._read_sessions()}
        if action == "browser.profiles.list":
            return {"action": action, "profiles": self._list_profiles(), "active_profile_id": self._active_profile_id()}
        if action == "browser.profile.create":
            return self._create_profile(payload)
        if action == "browser.profile.set_active":
            return self._set_active_profile(payload)
        if action == "browser.profile.delete":
            return self._delete_profile(payload, dry_run=self._truthy(payload.get("dry_run")), yolo_mode=yolo_mode)
        if action == "browser.profile.clear_cache":
            return self._clear_cache(payload, dry_run=self._truthy(payload.get("dry_run")), yolo_mode=yolo_mode)
        if action == "browser.profile.clear_cookies":
            return self._clear_cookies(payload, dry_run=self._truthy(payload.get("dry_run")), yolo_mode=yolo_mode)
        if action == "browser.cookies.list":
            return self._list_cookies(payload)
        if action == "browser.cookies.import":
            return self._import_cookies(payload)
        if action == "browser.cookies.delete":
            return self._delete_cookies(payload, dry_run=self._truthy(payload.get("dry_run")), yolo_mode=yolo_mode)
        if action in {"computer.context", "computer.app_context", "computer.state"}:
            return self._context(payload)
        if action in {"computer.apps", "computer.list_apps", "computer.open_apps", "computer.applications"}:
            return self._apps(payload)
        if action in {"computer.windows", "computer.list_windows"}:
            return {"action": "computer.windows", "platform": platform.system(), "windows": self._list_windows()}
        if action == "computer.select_app":
            return self._select_app(payload)
        if action in {"computer.show_app", "computer.focus_app", "computer.activate_app"}:
            return self._show_app(payload)
        if action == "computer.select_window":
            return self._select_window(payload)
        if action == "computer.probe_text_control":
            return self._probe_semantic_text_control(payload)
        if action == "computer.screenshot":
            return self._screenshot(payload=payload, dry_run=self._truthy(payload.get("dry_run")), yolo_mode=yolo_mode)
        if action in {"computer.ocr", "computer.ax_tree"}:
            return self._computer_read_action(action, payload, yolo_mode=yolo_mode)
        if action in {"computer.clipboard", "computer.clipboard.get", "computer.clipboard.read"}:
            return self._clipboard_read(payload, yolo_mode=yolo_mode)
        if action in {"computer.clipboard.set", "computer.clipboard.write", "computer.clipboard.clear"}:
            return self._clipboard_write(action, payload, yolo_mode=yolo_mode)
        if action in {"computer.backspace", "computer.delete_back"}:
            payload = dict(payload)
            payload.setdefault("key", "backspace")
            result = self._desktop_action("computer.key", payload, yolo_mode=yolo_mode)
            result["action"] = "computer.backspace"
            result.setdefault("underlying_action", "computer.key")
            return result
        if action in {"computer.move", "computer.click", "computer.drag", "computer.type", "computer.key", "computer.scroll"}:
            return self._desktop_action(action, payload, yolo_mode=yolo_mode)
        if action == "computer.observe":
            return self._computer_seat_observe(payload, yolo_mode=yolo_mode)
        if action == "computer.click_text":
            return self._computer_click_text(payload, yolo_mode=yolo_mode)
        if action in {"computer.semantic_action", "computer.press"}:
            return self._computer_seat_semantic_action(payload, yolo_mode=yolo_mode)
        if action == "computer.pid_event":
            return self._computer_seat_pid_event(payload, yolo_mode=yolo_mode)
        if action in {"computer.doctor", "computer.diagnose"}:
            return self._computer_seat_doctor()
        raise ValueError(f"Unsupported browser/computer action: {action}")

    @staticmethod
    def _normalize_action(action: str) -> str:
        raw = str(action or "").strip()
        action_map = {
            "": "browser.session",
            "session": "browser.session",
            "open_url": "browser.open_url",
            "browser_open_url": "browser.open_url",
            "context": "computer.context",
            "app_context": "computer.context",
            "state": "computer.context",
            "screenshot": "computer.screenshot",
            "ocr": "computer.ocr",
            "computer_ocr": "computer.ocr",
            "ax_tree": "computer.ax_tree",
            "accessibility_tree": "computer.ax_tree",
            "computer_ax_tree": "computer.ax_tree",
            "click_text": "computer.click_text",
            "text_click": "computer.click_text",
            "click_by_text": "computer.click_text",
            "computer_click_text": "computer.click_text",
            "move": "computer.move",
            "cursor_move": "computer.move",
            "mouse_move": "computer.move",
            "click": "computer.click",
            "drag": "computer.drag",
            "mouse_drag": "computer.drag",
            "type": "computer.type",
            "key": "computer.key",
            "backspace": "computer.backspace",
            "delete_back": "computer.backspace",
            "scroll": "computer.scroll",
            "clipboard": "computer.clipboard.read",
            "clipboard_read": "computer.clipboard.read",
            "clipboard_get": "computer.clipboard.read",
            "clipboard_write": "computer.clipboard.write",
            "clipboard_set": "computer.clipboard.write",
            "clipboard_clear": "computer.clipboard.clear",
            "apps": "computer.apps",
            "applications": "computer.apps",
            "open_apps": "computer.apps",
            "list_apps": "computer.apps",
            "select_app": "computer.select_app",
            "app": "computer.select_app",
            "show_app": "computer.show_app",
            "focus_app": "computer.show_app",
            "activate_app": "computer.show_app",
            "main_app": "computer.show_app",
            "show": "computer.show_app",
            "select_window": "computer.select_window",
            "window": "computer.select_window",
            "probe_text_control": "computer.probe_text_control",
            "probe_browser_address": "computer.probe_text_control",
            "windows": "computer.windows",
            "list_windows": "computer.windows",
            "observe": "computer.observe",
            "semantic_action": "computer.semantic_action",
            "press": "computer.semantic_action",
            "pid_event": "computer.pid_event",
            "doctor": "computer.doctor",
            "diagnose": "computer.doctor",
        }
        return action_map.get(raw, raw)

    @contextlib.contextmanager
    def _edge_haze(self, action: str, payload: dict[str, Any]):
        metadata: dict[str, Any] = {"attempted": True, "action": action}
        if str(os.environ.get("RUMI_EDGE_HAZE_DISABLED") or "").strip().lower() in {"1", "true", "yes", "on"}:
            metadata["started"] = False
            metadata["disabled"] = True
            yield metadata
            return
        manager: Any | None = None
        try:
            from ..computer.mac.edge_haze import ComputerUseEdgeHazeManager

            pack_root = Path(__file__).resolve().parents[2]
            manager = ComputerUseEdgeHazeManager.from_pack_root(pack_root)
            haze_payload = self._edge_haze_payload(action, payload)
            started = manager.start(action=action, payload=haze_payload)
            metadata["started"] = bool(started)
            target_window = haze_payload.get("edge_haze_target_window")
            if isinstance(target_window, dict) and target_window:
                metadata["target_window"] = target_window
            lease_path = getattr(manager, "_lease_path", None)
            sequence_id = getattr(manager, "_sequence_id", None)
            if lease_path is not None:
                metadata["lease_path"] = str(lease_path)
            if sequence_id:
                metadata["sequence_id"] = str(sequence_id)
        except Exception:
            metadata["started"] = False
            yield metadata
            return
        try:
            yield metadata
        finally:
            try:
                manager.stop()
            except Exception as exc:
                metadata["stop_error"] = str(exc)

    @staticmethod
    def _edge_haze_result(edge_haze: Any) -> dict[str, Any] | None:
        if not isinstance(edge_haze, dict):
            return None
        result: dict[str, Any] = {
            "attempted": bool(edge_haze.get("attempted")),
            "started": bool(edge_haze.get("started")),
        }
        for key in ("action", "sequence_id", "lease_path", "stop_error"):
            value = edge_haze.get(key)
            if isinstance(value, str) and value:
                result[key] = value
        if edge_haze.get("disabled") is True:
            result["disabled"] = True
        target_window = edge_haze.get("target_window")
        if isinstance(target_window, dict) and target_window:
            result["target_window"] = target_window
        return result

    @classmethod
    def _attach_edge_haze_result(cls, result: dict[str, Any], edge_haze: Any) -> None:
        metadata = cls._edge_haze_result(edge_haze)
        if metadata is not None:
            result["edge_haze"] = metadata

    def _edge_haze_payload(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        haze_payload = dict(payload or {})
        target_window = self._edge_haze_target_window(action, haze_payload)
        if target_window:
            haze_payload["edge_haze_target_window"] = target_window
        return haze_payload

    def _edge_haze_target_window(self, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        explicit = payload.get("edge_haze_target_window")
        if isinstance(explicit, dict):
            target = self._edge_haze_target_from_mapping(explicit, fallback=payload)
            if target:
                return target
        window_payload = payload.get("window")
        if isinstance(window_payload, dict):
            target = self._edge_haze_target_from_mapping(window_payload, fallback=payload)
            if target:
                return target
        target = self._edge_haze_target_from_mapping(payload)
        if target:
            return target
        state = self._computer_state()
        if not self._state_matches_artifact_root(state):
            state = {}
        selected = state.get("target_window") if isinstance(state.get("target_window"), dict) else None
        if selected:
            target = self._edge_haze_target_from_mapping(selected, fallback=payload)
            if target:
                return target
        return None

    @classmethod
    def _edge_haze_target_from_mapping(
        cls,
        value: dict[str, Any],
        *,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        fallback = fallback or {}
        if not isinstance(value, dict):
            return None
        target: dict[str, Any] = {}
        app = str(
            value.get("app")
            or value.get("name")
            or value.get("process")
            or fallback.get("app")
            or fallback.get("application")
            or fallback.get("target_app")
            or ""
        ).strip()
        if app:
            target["app"] = app[:160]
        title = str(
            value.get("window_title")
            or value.get("title")
            or fallback.get("window_title")
            or fallback.get("title")
            or ""
        ).strip()
        if title:
            target["window_title"] = title[:240]
        for source_key, output_key in (
            ("pid", "pid"),
            ("window_id", "window_id"),
            ("id", "window_id"),
            ("x", "x"),
            ("y", "y"),
            ("width", "width"),
            ("height", "height"),
        ):
            if output_key in target:
                continue
            raw_value = value.get(source_key)
            if raw_value in (None, ""):
                raw_value = fallback.get(source_key)
            parsed = cls._optional_int(raw_value)
            if parsed is None:
                continue
            if output_key in {"pid", "window_id", "width", "height"} and parsed <= 0:
                continue
            target[output_key] = parsed
        frame_ids = value.get("frame_window_ids")
        if isinstance(frame_ids, list):
            ids = [item for item in (cls._optional_int(frame_id) for frame_id in frame_ids) if item and item > 0]
            if ids:
                target["frame_window_ids"] = ids[:16]
        has_identifier = any(
            target.get(key) not in (None, "", [])
            for key in ("app", "window_title", "pid", "window_id", "frame_window_ids")
        )
        return target if has_identifier else None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(float(value))
        except Exception:
            return None

    def _open_url(self, url: str, *, payload: dict[str, Any], dry_run: bool, yolo_mode: bool) -> dict[str, Any]:
        if not url.startswith(("http://", "https://", "file://")):
            raise ValueError("'url' must start with http://, https://, or file://")
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id") or self._active_profile_id())
        persistent = payload.get("persistent", True) is not False
        target_app = self._app_name_from_payload(payload)
        launch_plan = self._browser_launch_plan(url, profile_id, persistent=persistent)
        if target_app and platform.system() == "Darwin":
            launch_plan = {
                "mode": "target_app",
                "target_app": target_app,
                "commands": self._darwin_open_url_commands(url, target_app),
            }
        if dry_run:
            return {
                "action": "browser.open_url",
                "url": url,
                "profile_id": profile_id,
                "persistent": persistent,
                "dry_run": True,
                "requires_approval": False,
                "launch": launch_plan,
                **({"target_app": target_app} if target_app else {}),
            }
        approval_payload = {"url": url, "profile_id": profile_id, "persistent": persistent, "target_app": target_app}
        approved = yolo_mode or self._consume_approval(payload, "browser.open_url", approval_payload)
        if not approved:
            return self._approval_required("browser.open_url", approval_payload)
        self._ensure_profile(profile_id)
        opened_with_managed_profile = False
        open_details: dict[str, Any] = {}
        if persistent and launch_plan.get("command") and not target_app:
            command = [str(part) for part in launch_plan["command"]]
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened_with_managed_profile = True
            edge_haze = None
        else:
            with self._edge_haze("browser.open_url", payload) as edge_haze:
                open_result = self._open_url_result(url, app_name=target_app)
            open_details = {
                key: value
                for key, value in open_result.items()
                if key not in {"opened", "reason"} and value is not None
            }
            if not open_result.get("opened"):
                return {
                    "action": "browser.open_url",
                    "url": url,
                    "opened": False,
                    "is_error": True,
                    "profile_id": profile_id,
                    "persistent": persistent,
                    **({"target_app": target_app} if target_app else {}),
                    **open_details,
                    "reason": str(open_result.get("reason") or "Opening the requested URL failed."),
                }
        sessions = self._read_sessions()
        sessions["last_url"] = url
        sessions["active_profile_id"] = profile_id
        sessions["last_opened_with_managed_profile"] = opened_with_managed_profile
        for stale_key in ("last_opened_background", "browser_target", "chrome_target"):
            sessions.pop(stale_key, None)
        sessions["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._write_sessions(sessions)
        result = {
            "action": "browser.open_url",
            "url": url,
            "opened": True,
            "profile_id": profile_id,
            "persistent": persistent,
            "managed_profile": opened_with_managed_profile,
            "launch": launch_plan,
            **open_details,
            **({"edge_haze": metadata} if (metadata := self._edge_haze_result(edge_haze)) else {}),
            **({"target_app": target_app} if target_app else {}),
        }
        return self._with_browser_text_input_recommendations(result)

    def _open_url_result(self, url: str, *, app_name: str = "") -> dict[str, Any]:
        if platform.system() == "Darwin" and app_name:
            return self._darwin_open_url_with_target_app(url, app_name)
        opened = self._open_url_foreground(url, app_name=app_name)
        if opened:
            return {"opened": True}
        reason = "Opening the requested URL failed."
        if app_name:
            reason = f"Opening the requested URL in {app_name} failed."
        return {"opened": False, "reason": reason}

    def _darwin_open_url_with_target_app(self, url: str, app_name: str) -> dict[str, Any]:
        app_name = app_name.strip()
        if not app_name:
            return {"opened": False, "reason": "No target app was provided for the macOS browser launch."}
        failures: list[str] = []
        accepted_state: dict[str, Any] | None = None
        accepted_command: list[str] | None = None
        for command in self._darwin_open_url_commands(url, app_name):
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                failures.append(f"{' '.join(command)} timed out")
                continue
            except FileNotFoundError:
                return {"opened": False, "reason": "The macOS open command is not available."}
            except Exception as exc:
                failures.append(f"{' '.join(command)} failed: {exc}")
                continue
            if completed.returncode != 0:
                detail = str(completed.stderr or completed.stdout or "").strip()
                failures.append(f"{' '.join(command)} failed{': ' + detail if detail else ''}")
                continue

            if self._darwin_targeted_open_accepts_command_success(app_name):
                state = self._darwin_target_app_state(app_name)
                return {
                    "opened": True,
                    "launch_command": command,
                    "command_accepted": True,
                    "window_verified": bool(state.get("available")),
                    **state,
                }

            state = self._wait_for_darwin_target_app(app_name)
            if state.get("available"):
                return {"opened": True, "launch_command": command, **state}
            activated = self._activate_app_name(app_name)
            if activated:
                state = self._wait_for_darwin_target_app(app_name, timeout=0.5)
                if state.get("available"):
                    return {"opened": True, "activated": True, "launch_command": command, **state}
            accepted_state = state
            accepted_command = command

        if accepted_state is not None:
            if accepted_state.get("running_app"):
                return {
                    "opened": False,
                    "reason": f"macOS accepted the open request and {app_name} is running, but no usable window became available.",
                    "launch_command": accepted_command,
                    "running_app": accepted_state.get("running_app"),
                }
            return {
                "opened": False,
                "reason": f"macOS accepted the open request, but {app_name} did not become available.",
                "launch_command": accepted_command,
            }
        reason = f"macOS could not open the requested URL in {app_name}."
        if failures:
            reason = f"{reason} {'; '.join(failures)}"
        return {"opened": False, "reason": reason}

    @staticmethod
    def _darwin_targeted_open_accepts_command_success(app_name: str) -> bool:
        if not _truthy_env("RUMI_COMPUTER_USE_DEBUG_FOREGROUND"):
            return False
        key = app_name.strip().lower()
        return key in _DARWIN_BROWSER_BUNDLE_ID_ALIASES

    @staticmethod
    def _darwin_open_url_commands(url: str, app_name: str) -> list[list[str]]:
        key = app_name.strip().lower()
        commands = [["open", "-b", bundle_id, url] for bundle_id in _DARWIN_BROWSER_BUNDLE_ID_ALIASES.get(key, ())]
        commands.append(["open", "-a", app_name, url])
        return commands

    def _wait_for_darwin_target_app(
        self,
        app_name: str,
        *,
        timeout: float = _DARWIN_AUTOMATION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            state = self._darwin_target_app_state(app_name)
            if state.get("available") or time.monotonic() >= deadline:
                return state
            time.sleep(0.1)

    def _darwin_target_app_state(self, app_name: str) -> dict[str, Any]:
        active_window = self._active_window_for_app(app_name)
        if active_window is not None:
            return {"available": True, "active_window": active_window}
        running_app = next(
            (item for item in self._running_apps() if self._app_matches_filter(item, app_name)),
            None,
        )
        if running_app is not None:
            return {"available": False, "running_app": running_app}
        return {"available": False}

    @staticmethod
    def _open_url_foreground(url: str, *, app_name: str = "") -> bool:
        if platform.system() == "Darwin":
            try:
                command = ["open"]
                if app_name:
                    command.extend(["-a", app_name])
                command.append(url)
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
        if platform.system() == "Windows" and app_name:
            return BrowserComputerController._windows_open_url_foreground(url, app_name)
        if app_name:
            return False
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False

    def _create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("name") or f"profile-{int(time.time())}")
        label = str(payload.get("label") or payload.get("name") or profile_id)
        profile = self._ensure_profile(profile_id, label=label)
        if payload.get("set_active", True) is not False:
            sessions = self._read_sessions()
            sessions["active_profile_id"] = profile_id
            sessions["updated_at"] = self._now_iso()
            self._write_sessions(sessions)
        return {"action": "browser.profile.create", "profile": self._profile_summary(profile_id, profile), "active_profile_id": self._active_profile_id()}

    def _set_active_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id"))
        self._ensure_profile(profile_id)
        sessions = self._read_sessions()
        sessions["active_profile_id"] = profile_id
        sessions["updated_at"] = self._now_iso()
        self._write_sessions(sessions)
        return {"action": "browser.profile.set_active", "active_profile_id": profile_id}

    def _delete_profile(self, payload: dict[str, Any], *, dry_run: bool, yolo_mode: bool) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id"))
        if profile_id == "default":
            raise ValueError("The default browser profile cannot be deleted.")
        profile_path = self._profile_path(profile_id)
        approval_payload = {"profile_id": profile_id}
        if dry_run:
            return {
                "action": "browser.profile.delete",
                "profile_id": profile_id,
                "dry_run": True,
                "requires_approval": False,
                "exists": profile_path.exists(),
            }
        if not (yolo_mode or self._consume_approval(payload, "browser.profile.delete", approval_payload)):
            return self._approval_required("browser.profile.delete", approval_payload)
        shutil.rmtree(profile_path, ignore_errors=True)
        sessions = self._read_sessions()
        profiles = sessions.get("profiles") if isinstance(sessions.get("profiles"), dict) else {}
        profiles.pop(profile_id, None)
        sessions["profiles"] = profiles
        if sessions.get("active_profile_id") == profile_id:
            sessions["active_profile_id"] = "default"
        sessions["updated_at"] = self._now_iso()
        self._write_sessions(sessions)
        return {"action": "browser.profile.delete", "profile_id": profile_id, "deleted": True}

    def _clear_cache(self, payload: dict[str, Any], *, dry_run: bool, yolo_mode: bool) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id") or self._active_profile_id())
        self._ensure_profile(profile_id)
        candidates = self._cache_paths(profile_id)
        existing = [path for path in candidates if path.exists()]
        approval_payload = {"profile_id": profile_id}
        if dry_run:
            return {
                "action": "browser.profile.clear_cache",
                "profile_id": profile_id,
                "dry_run": True,
                "requires_approval": False,
                "paths": [str(path) for path in existing],
                "size_bytes": sum(self._path_size(path) for path in existing),
            }
        if not (yolo_mode or self._consume_approval(payload, "browser.profile.clear_cache", approval_payload)):
            return self._approval_required("browser.profile.clear_cache", approval_payload)
        removed = [str(path) for path in existing if self._remove_path(path)]
        return {"action": "browser.profile.clear_cache", "profile_id": profile_id, "removed": removed}

    def _clear_cookies(self, payload: dict[str, Any], *, dry_run: bool, yolo_mode: bool) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id") or self._active_profile_id())
        self._ensure_profile(profile_id)
        include_managed = payload.get("include_managed", True) is not False
        candidates = self._browser_cookie_paths(profile_id)
        if include_managed:
            candidates.append(self._cookie_jar_path(profile_id))
        existing = [path for path in candidates if path.exists()]
        approval_payload = {"profile_id": profile_id, "include_managed": include_managed}
        if dry_run:
            return {
                "action": "browser.profile.clear_cookies",
                "profile_id": profile_id,
                "dry_run": True,
                "requires_approval": False,
                "paths": [str(path) for path in existing],
            }
        if not (yolo_mode or self._consume_approval(payload, "browser.profile.clear_cookies", approval_payload)):
            return self._approval_required("browser.profile.clear_cookies", approval_payload)
        removed = [str(path) for path in existing if self._remove_path(path)]
        return {"action": "browser.profile.clear_cookies", "profile_id": profile_id, "removed": removed}

    def _list_cookies(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id") or self._active_profile_id())
        jar = self._read_cookie_jar(profile_id)
        include_values = bool(payload.get("include_values"))
        cookies = [self._cookie_public_view(cookie, include_values=include_values) for cookie in jar.get("cookies", [])]
        return {"action": "browser.cookies.list", "profile_id": profile_id, "cookies": cookies, "count": len(cookies)}

    def _import_cookies(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id") or self._active_profile_id())
        cookies = payload.get("cookies")
        if not isinstance(cookies, list):
            raise ValueError("'cookies' must be a list")
        normalized = [self._normalize_cookie(cookie) for cookie in cookies if isinstance(cookie, dict)]
        replace = bool(payload.get("replace"))
        current = [] if replace else list(self._read_cookie_jar(profile_id).get("cookies", []))
        merged = self._merge_cookies(current, normalized)
        self._write_cookie_jar(profile_id, {"version": 1, "cookies": merged, "updated_at": self._now_iso()})
        self._ensure_profile(profile_id)
        return {"action": "browser.cookies.import", "profile_id": profile_id, "imported": len(normalized), "count": len(merged)}

    def _delete_cookies(self, payload: dict[str, Any], *, dry_run: bool, yolo_mode: bool) -> dict[str, Any]:
        profile_id = self._profile_id(payload.get("profile_id") or payload.get("session_id") or self._active_profile_id())
        name = str(payload.get("name") or "")
        domain = str(payload.get("domain") or "")
        path = str(payload.get("path") or "")
        approval_payload = {"profile_id": profile_id, "name": name, "domain": domain, "path": path}
        jar = self._read_cookie_jar(profile_id)
        cookies = list(jar.get("cookies", []))
        matches = [cookie for cookie in cookies if self._cookie_matches(cookie, name=name, domain=domain, path=path)]
        if dry_run:
            return {
                "action": "browser.cookies.delete",
                "profile_id": profile_id,
                "dry_run": True,
                "requires_approval": False,
                "matches": len(matches),
            }
        if not (yolo_mode or self._consume_approval(payload, "browser.cookies.delete", approval_payload)):
            return self._approval_required("browser.cookies.delete", approval_payload)
        remaining = [cookie for cookie in cookies if not self._cookie_matches(cookie, name=name, domain=domain, path=path)]
        self._write_cookie_jar(profile_id, {"version": 1, "cookies": remaining, "updated_at": self._now_iso()})
        return {"action": "browser.cookies.delete", "profile_id": profile_id, "deleted": len(cookies) - len(remaining), "count": len(remaining)}

    def _screenshot(self, *, payload: dict[str, Any], dry_run: bool, yolo_mode: bool) -> dict[str, Any]:
        if dry_run:
            return {
                "action": "computer.screenshot",
                "dry_run": True,
                "requires_approval": False,
                "target_window": self._capture_target(payload),
            }
        approval_payload = self._safe_payload(payload)
        if not (yolo_mode or self._consume_approval(payload, "computer.screenshot", approval_payload)):
            return self._approval_required("computer.screenshot", approval_payload)
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        path = self._artifact_root / f"screenshot-{int(time.time() * 1000)}.png"
        target_binding_source = self._screenshot_target_binding_source(payload)
        target_required = target_binding_source != "none"
        try:
            capture = self._capture_or_reuse_screenshot(path, payload)
        except Exception:
            return self._screenshot_failure(
                "SCREENSHOT_CAPTURE_FAILED",
                failure_stage="native_capture",
                screenshot_supported=platform.system() in {"Darwin", "Windows", "Linux"},
                target_resolved=not target_required,
                capture_attempted=True,
                capture_driver=self._screenshot_capture_driver(platform.system(), None, payload),
                target_binding_source=target_binding_source,
            )
        system = capture.get("platform", platform.system())
        if target_binding_source == "none" and isinstance(capture.get("target_window"), dict):
            target_binding_source = "persisted_selection"
            target_required = True
        capture_driver = self._screenshot_capture_driver(system, capture, payload)
        target_resolved = not target_required or isinstance(capture.get("target_window"), dict)
        if not capture.get("supported", True):
            target_unavailable = capture.get("target_filter") is not None or (target_required and not target_resolved)
            platform_supported = system in {"Darwin", "Windows", "Linux"}
            return self._screenshot_failure(
                "SCREENSHOT_TARGET_UNAVAILABLE"
                if target_unavailable
                else "SCREENSHOT_PLATFORM_UNSUPPORTED"
                if not platform_supported
                else "SCREENSHOT_CAPTURE_FAILED",
                failure_stage="target_resolution" if target_unavailable else "native_capture",
                screenshot_supported=platform_supported,
                target_resolved=target_resolved,
                capture_attempted=not target_unavailable and platform_supported,
                capture_driver="none" if target_unavailable else capture_driver,
                target_binding_source=target_binding_source,
            )
        if target_required and not target_resolved:
            return self._screenshot_failure(
                "SCREENSHOT_TARGET_UNAVAILABLE",
                failure_stage="target_resolution",
                screenshot_supported=True,
                target_resolved=False,
                capture_attempted=False,
                capture_driver="none",
                target_binding_source=target_binding_source,
            )
        crop_result = self._apply_screenshot_crop(path, payload, capture)
        crop_reference = crop_result.get("crop_reference") if crop_result else None
        action_target = crop_result.get("action_target") if crop_result else capture.get("action_coordinate_system")
        if crop_result and isinstance(crop_result.get("path"), Path):
            path = crop_result["path"]
        artifact_contract = self._screenshot_artifact_contract(path)
        if not artifact_contract["artifact_file_created"]:
            return self._screenshot_failure(
                "SCREENSHOT_ARTIFACT_OUTSIDE_ROOT"
                if not artifact_contract["artifact_root_match"]
                else "SCREENSHOT_ARTIFACT_NOT_CREATED",
                failure_stage="artifact_validation",
                screenshot_supported=True,
                target_resolved=target_resolved,
                capture_attempted=True,
                capture_succeeded=True,
                capture_driver=capture_driver,
                target_binding_source=target_binding_source,
                **artifact_contract,
            )
        try:
            model_path = self._model_screenshot_copy(path)
        except Exception:
            return self._screenshot_failure(
                "SCREENSHOT_MODEL_ARTIFACT_NOT_CREATED",
                failure_stage="model_copy",
                screenshot_supported=True,
                target_resolved=target_resolved,
                capture_attempted=True,
                capture_succeeded=True,
                capture_driver=capture_driver,
                target_binding_source=target_binding_source,
                **artifact_contract,
            )
        model_contract = self._screenshot_artifact_contract(model_path, model=True)
        if not model_contract["model_file_created"]:
            failed_contract = dict(artifact_contract)
            failed_contract.update(model_contract)
            return self._screenshot_failure(
                "SCREENSHOT_ARTIFACT_OUTSIDE_ROOT"
                if not model_contract["artifact_root_match"]
                else "SCREENSHOT_MODEL_ARTIFACT_NOT_CREATED",
                failure_stage="model_copy",
                screenshot_supported=True,
                target_resolved=target_resolved,
                capture_attempted=True,
                capture_succeeded=True,
                capture_driver=capture_driver,
                target_binding_source=target_binding_source,
                **failed_contract,
            )
        data_url = self._image_data_url(model_path)
        result = self._screenshot_result(
            path,
            model_path,
            system,
            capture_target=capture.get("target_window"),
            action_target=action_target,
            crop_reference=crop_reference,
        )
        result.update(
            {
                "screenshot_path": str(path),
                "screenshot_supported": True,
                "target_resolved": target_resolved,
                "capture_attempted": True,
                "capture_succeeded": True,
                "artifact_path_present": True,
                "model_path_present": True,
                "artifact_file_created": True,
                "model_file_created": True,
                "artifact_root_match": True,
                "screenshot_contract_valid": True,
                "capture_driver": capture_driver,
                "target_binding_source": target_binding_source,
            }
        )
        if data_url:
            result["data_url"] = data_url
            result["model_image"] = data_url
            result["model_image_path"] = str(model_path)
        # Additive ComputerSeat metadata
        try:
            svc = self._get_computer_seat()
            doctor = svc.doctor()
            result["computer_seat"] = {
                "driver_chain_order": doctor.get("driver_chain_order", []),
                "capabilities": [d.get("capabilities", {}) for d in doctor.get("available_drivers", [])],
            }
        except Exception:
            pass
        return result

    def _screenshot_artifact_contract(self, path: Path, *, model: bool = False) -> dict[str, bool]:
        path_present_key = "model_path_present" if model else "artifact_path_present"
        file_created_key = "model_file_created" if model else "artifact_file_created"
        facts = {
            path_present_key: bool(str(path)),
            file_created_key: False,
            "artifact_root_match": False,
            "artifact_symlink": False,
            "artifact_regular_file": False,
            "artifact_nonempty": False,
        }
        try:
            facts["artifact_symlink"] = path.is_symlink()
            root = self._artifact_root.expanduser().resolve()
            resolved = path.expanduser().resolve()
            facts["artifact_root_match"] = resolved.is_relative_to(root)
            details = path.lstat()
            facts["artifact_regular_file"] = stat.S_ISREG(details.st_mode) and not facts["artifact_symlink"]
            facts["artifact_nonempty"] = details.st_size > 0
            facts[file_created_key] = bool(
                facts["artifact_root_match"]
                and facts["artifact_regular_file"]
                and facts["artifact_nonempty"]
            )
        except (OSError, RuntimeError, ValueError):
            pass
        return facts

    @staticmethod
    def _screenshot_target_binding_source(payload: dict[str, Any]) -> str:
        target = str(payload.get("target") or payload.get("capture_target") or "").strip().lower()
        if isinstance(payload.get("window"), dict):
            return "explicit_window"
        if any(payload.get(key) not in (None, "") for key in ("pid", "window_id", "hwnd")):
            return "explicit_identifiers"
        if any(str(payload.get(key) or "").strip() for key in ("app", "application", "title", "window_title", "title_contains")):
            return "enumerated_match"
        if target in {"active_window", "front_window"}:
            return "active_window"
        if target in {"selected_window", "window", "app"} or not target:
            return "persisted_selection" if target else "none"
        return "none"

    @staticmethod
    def _screenshot_capture_driver(system: str, capture: dict[str, Any] | None, payload: dict[str, Any]) -> str:
        reported = str((capture or {}).get("driver") or "")
        if reported in _SCREENSHOT_CAPTURE_DRIVERS:
            return reported
        if system == "Darwin":
            target = (capture or {}).get("target_window")
            if isinstance(target, dict):
                if target.get("capture_rect"):
                    return "mac_screencapture_rect"
                if target.get("window_id") not in (None, ""):
                    return "mac_screencapture_window"
                return "mac_screencapture_rect"
            return "mac_screencapture_display"
        if system == "Windows":
            return "windows_native"
        if system == "Linux":
            return "linux_native"
        return "none"

    @staticmethod
    def _screenshot_failure(
        error_code: str,
        *,
        failure_stage: str,
        screenshot_supported: bool,
        target_resolved: bool,
        capture_attempted: bool,
        capture_driver: str,
        target_binding_source: str,
        capture_succeeded: bool = False,
        **artifact_facts: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": "computer.screenshot",
            "is_error": True,
            "supported": screenshot_supported,
            "error_code": error_code,
            "screenshot_supported": screenshot_supported,
            "target_resolved": target_resolved,
            "capture_attempted": capture_attempted,
            "capture_succeeded": capture_succeeded,
            "artifact_path_present": bool(artifact_facts.get("artifact_path_present")),
            "model_path_present": bool(artifact_facts.get("model_path_present")),
            "artifact_file_created": bool(artifact_facts.get("artifact_file_created")),
            "model_file_created": bool(artifact_facts.get("model_file_created")),
            "artifact_root_match": bool(artifact_facts.get("artifact_root_match")),
            "screenshot_contract_valid": False,
            "artifact_symlink": bool(artifact_facts.get("artifact_symlink")),
            "artifact_regular_file": bool(artifact_facts.get("artifact_regular_file")),
            "artifact_nonempty": bool(artifact_facts.get("artifact_nonempty")),
            "capture_driver": capture_driver if capture_driver in _SCREENSHOT_CAPTURE_DRIVERS else "none",
            "target_binding_source": (
                target_binding_source if target_binding_source in _SCREENSHOT_TARGET_BINDING_SOURCES else "none"
            ),
            "failure_stage": failure_stage,
        }
        return result

    def _screenshot_result(
        self,
        path: Path,
        model_path: Path,
        system: str,
        *,
        capture_target: dict[str, Any] | None = None,
        action_target: dict[str, Any] | None = None,
        crop_reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": "computer.screenshot",
            "path": str(path),
            "model_image_path": str(model_path),
            "mime_type": "image/png",
            "platform": system,
        }
        image_size = self._image_size(path)
        model_image_size = self._image_size(model_path)
        if capture_target:
            result["target_window"] = capture_target
        if crop_reference:
            result["crop_reference"] = crop_reference
        if image_size:
            width, height = image_size
            result["image_size"] = {"width": width, "height": height}
            result["coordinate_system"] = {
                "origin": "top_left",
                "unit": "px",
                "space": "screenshot_image",
                "x_range": [0, max(width - 1, 0)],
                "y_range": [0, max(height - 1, 0)],
            }
        try:
            action_coordinate_system = self._action_coordinate_system(system, image_size, capture_target=action_target or capture_target)
        except TypeError:
            action_coordinate_system = self._action_coordinate_system(system, image_size)
        if action_coordinate_system:
            result["action_coordinate_system"] = action_coordinate_system
        if model_image_size:
            model_width, model_height = model_image_size
            result["model_image_size"] = {"width": model_width, "height": model_height}
        if image_size and model_image_size and model_image_size[0] and model_image_size[1]:
            result["model_to_screen_scale"] = {
                "x": image_size[0] / model_image_size[0],
                "y": image_size[1] / model_image_size[1],
            }
        if action_coordinate_system and model_image_size and model_image_size[0] and model_image_size[1]:
            action_width = action_coordinate_system.get("width")
            action_height = action_coordinate_system.get("height")
            if action_width and action_height:
                result["model_to_action_scale"] = {
                    "x": action_width / model_image_size[0],
                    "y": action_height / model_image_size[1],
                }
                result["model_to_action_scale_legacy"] = True
        if action_coordinate_system and image_size and image_size[0] and image_size[1]:
            action_width = action_coordinate_system.get("width")
            action_height = action_coordinate_system.get("height")
            if action_width and action_height:
                result["screenshot_to_action_scale"] = {
                    "x": action_width / image_size[0],
                    "y": action_height / image_size[1],
                }
        cursor = self._cursor_position()
        if cursor:
            result["cursor"] = cursor
        context = self._context({"include_windows": False})
        if context.get("ai_cursor"):
            result["ai_cursor"] = context.get("ai_cursor")
        if context.get("active_window"):
            result["active_window"] = context.get("active_window")
        if context.get("selected_window"):
            result["selected_window"] = context.get("selected_window")
        result["coordinate_contract"] = self._coordinate_contract(crop_reference=crop_reference)
        result["cursor_move_contract"] = {
            "tool": "browser_use",
            "action": "move",
            "screen_coordinates": True,
            "coordinate_source": "attached_image",
            "notes": "For image-based clicking, prefer normalized_x and normalized_y with coordinate_space=normalized_1000. Values are clamped to 0-1000 relative to the attached image; the harness converts them to action pixels. point:[y,x] and normalized_point remain legacy-compatible for normalized_1000 only.",
        }
        self._with_browser_text_input_recommendations(result)
        self._remember_last_screenshot(result)
        return result

    @staticmethod
    def _with_browser_text_input_recommendations(result: dict[str, Any]) -> dict[str, Any]:
        existing = result.get("recommended_next_actions")
        recommendations = list(existing) if isinstance(existing, list) else []
        for action in _BROWSER_TEXT_INPUT_RECOMMENDED_NEXT_ACTIONS:
            if action not in recommendations:
                recommendations.append(action)
        result["recommended_next_actions"] = recommendations
        result.setdefault("input_guidance", _BROWSER_TEXT_INPUT_GUIDANCE)
        return result

    @staticmethod
    def _with_computer_type_success_guidance(result: dict[str, Any]) -> dict[str, Any]:
        existing = result.get("recommended_next_actions")
        recommendations = list(existing) if isinstance(existing, list) else []
        for action in _COMPUTER_TYPE_SUCCESS_RECOMMENDED_NEXT_ACTIONS:
            if action not in recommendations:
                recommendations.append(action)
        result["recommended_next_actions"] = recommendations
        result["input_guidance"] = _COMPUTER_TYPE_SUCCESS_GUIDANCE
        result.setdefault("task_progress", dict(_COMPUTER_TYPE_SUCCESS_TASK_PROGRESS))
        preferred_next_action = dict(_COMPUTER_TYPE_SUCCESS_NEXT_ACTION["preferred"])
        preferred_next_action["payload"] = dict(preferred_next_action["payload"])
        result.setdefault(
            "next_action",
            {
                "preferred": preferred_next_action,
                "alternatives": [
                    dict(item) for item in _COMPUTER_TYPE_SUCCESS_NEXT_ACTION["alternatives"]
                ],
            },
        )
        result.setdefault(
            "avoid_actions",
            [dict(item) for item in _COMPUTER_TYPE_SUCCESS_AVOID_ACTIONS],
        )
        return result

    @staticmethod
    def _coordinate_contract(*, crop_reference: dict[str, Any] | None = None) -> dict[str, Any]:
        contract: dict[str, Any] = {
            "primary": "normalized_1000",
            "coordinate_space": "normalized_1000",
            "input_fields": ["normalized_x", "normalized_y"],
            "range": {"normalized_x": [0, 1000], "normalized_y": [0, 1000]},
            "origin": "top_left",
            "reference": "attached_image",
            "conversion": "The harness clamps normalized_x/normalized_y to 0..1000 and maps them to action pixels using width-1 and height-1.",
            "legacy_accepted": ["point:[y,x] with coordinate_space=normalized_1000", "normalized_point:[y,x]"],
            "legacy_scale_metadata": "model_to_action_scale may be present for old callers; models should not use it for coordinates.",
        }
        if crop_reference:
            contract["crop_reference"] = "normalized coordinates apply to the cropped/zoomed attached image, not the uncropped source."
        return contract

    def _context(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        state = self._computer_state()
        sessions = self._read_sessions()
        system = platform.system()
        selected_window = state.get("target_window") if isinstance(state.get("target_window"), dict) else None
        selected_app = state.get("target_app") if isinstance(state.get("target_app"), dict) else None
        if selected_window and not self._is_usable_target_window(selected_window):
            self._clear_target_window()
            selected_window = None
        running_apps = self._running_apps()
        result: dict[str, Any] = {
            "action": "computer.context",
            "platform": system,
            "active_window": self._active_window(),
            "selected_window": selected_window,
            "selected_app": selected_app,
            "open_apps": running_apps,
            "ai_cursor": state.get("ai_cursor") if isinstance(state.get("ai_cursor"), dict) else None,
            "cursor": self._cursor_position(),
            "browser_session": {
                "last_url": sessions.get("last_url"),
                "last_opened_with_managed_profile": bool(sessions.get("last_opened_with_managed_profile")),
            },
            "notes": [
                "Computer-use is app-generic and visible-screen only: use computer.apps for open/installed apps and computer.windows for visible windows.",
                "Use select_app/select_window for visible targets, then screenshot/click/type/key against the currently visible UI.",
                "computer.move, computer.click, and computer.drag use the virtual AI cursor by default; set physical=true only after explicit approval to operate the visible UI.",
                "Hidden tabs and DOM/Apple Events background input are disabled; if a requested app/window is not visible, ask the user to show it or open it visibly first.",
            ],
        }
        if payload.get("include_windows", True) is not False:
            result["windows"] = self._list_windows()
        if payload.get("include_installed_apps") is True:
            result["installed_apps"] = self._installed_apps(payload)
        # Additive ComputerSeat metadata
        try:
            svc = self._get_computer_seat()
            doctor = svc.doctor()
            result["computer_seat"] = {
                "driver_chain_order": doctor.get("driver_chain_order", []),
                "capabilities": [d.get("capabilities", {}) for d in doctor.get("available_drivers", [])],
                "recommended_next_actions": ["computer.screenshot", "computer.select_app", "computer.observe"],
            }
        except Exception:
            pass
        return result

    def _apps(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        scope = str(payload.get("scope") or payload.get("target") or "running").strip().lower()
        include_installed = payload.get("include_installed") is True or scope in {"all", "installed", "applications", "apps"}
        running_apps = self._running_apps()
        result: dict[str, Any] = {
            "action": "computer.apps",
            "platform": platform.system(),
            "scope": scope,
            "open_apps": running_apps,
            "apps": running_apps,
        }
        if include_installed:
            installed = self._installed_apps(payload)
            result["installed_apps"] = installed
            result["apps"] = installed if scope in {"installed", "applications"} else self._merge_apps(running_apps, installed)
        return result

    @staticmethod
    def _merge_apps(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in list(primary) + list(secondary):
            name = str(item.get("name") or item.get("app") or "").strip().lower()
            path = str(item.get("path") or "").strip().lower()
            key = (name, path)
            if not name or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _running_apps(self) -> list[dict[str, Any]]:
        system = platform.system()
        if system == "Darwin":
            swift_apps = self._darwin_swift_apps()
            if swift_apps:
                return swift_apps
            return self._darwin_running_apps()
        if system == "Windows":
            return self._windows_running_apps()
        if system == "Linux":
            try:
                from ..computer.linux import xdotool

                return xdotool.running_apps()
            except Exception:
                return []
        return []

    def _installed_apps(self, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = payload or {}
        try:
            limit = max(1, min(1000, int(payload.get("limit", 300))))
        except Exception:
            limit = 300
        system = platform.system()
        if system == "Darwin":
            return self._darwin_installed_apps(limit=limit)
        if system == "Windows":
            return self._windows_installed_apps(limit=limit)
        if system == "Linux":
            try:
                from ..computer.linux import xdotool

                return xdotool.installed_apps(limit=limit)
            except Exception:
                return []
        return []

    def _select_app(self, payload: dict[str, Any]) -> dict[str, Any]:
        app_filter = str(
            payload.get("app")
            or payload.get("application")
            or payload.get("name")
            or payload.get("title")
            or payload.get("title_contains")
            or ""
        ).strip()
        running_apps = self._running_apps()
        selected = None
        target = str(payload.get("target") or "").strip().lower()
        if target in {"active", "front", "front_app", "active_app"} or not app_filter:
            selected = next((item for item in running_apps if item.get("active")), None)
        if selected is None and app_filter:
            selected = next((item for item in running_apps if self._app_matches_filter(item, app_filter)), None)
        installed_match = None
        if selected is None and (payload.get("include_installed") is not False):
            installed = self._installed_apps(payload)
            installed_match = next((item for item in installed if self._app_matches_filter(item, app_filter)), None)
        else:
            installed = []
        if selected is None and installed_match and (payload.get("open") is True or payload.get("launch") is True):
            launched = self._launch_app(installed_match)
            if launched:
                time.sleep(0.5)
                running_apps = self._running_apps()
                selected = next((item for item in running_apps if self._app_matches_filter(item, app_filter)), None)
        if selected is None:
            self._clear_target_app()
            return {
                "action": "computer.select_app",
                "selected": False,
                "platform": platform.system(),
                "app_filter": app_filter,
                "open_apps": running_apps,
                **({"installed_match": installed_match} if installed_match else {}),
                **({"installed_apps": installed} if payload.get("include_installed") is True else {}),
            }
        selected = self._normalize_app_record(selected)
        state = self._computer_state()
        state["target_app"] = selected
        if payload.get("focus", True) is not False:
            active_window = self._active_window_for_app(str(selected.get("name") or selected.get("app") or ""))
            if active_window is not None:
                state["target_window"] = active_window
        self._write_computer_state(state)
        if payload.get("focus", True) is not False:
            self._activate_app_name(str(selected.get("name") or selected.get("app") or ""))
            active_window = self._active_window_for_app(str(selected.get("name") or selected.get("app") or ""))
            if active_window is not None:
                state = self._computer_state()
                state["target_window"] = active_window
                self._write_computer_state(state)
        return {
            "action": "computer.select_app",
            "selected": True,
            "platform": platform.system(),
            "target_app": selected,
            "open_apps": running_apps,
            "computer_seat": self._computer_seat_metadata_for_target(selected),
        }

    def _show_app(self, payload: dict[str, Any]) -> dict[str, Any]:
        action_payload = dict(payload or {})
        action_payload["focus"] = True
        if action_payload.get("open") is not False and action_payload.get("launch") is not False:
            action_payload.setdefault("open", True)
        selected_window = self._matching_window(action_payload) if self._has_window_filter(action_payload) else None
        if selected_window is not None:
            state = self._computer_state()
            state["target_window"] = selected_window
            state["target_app"] = {"name": selected_window.get("app"), "app": selected_window.get("app"), "running": True}
            self._write_computer_state(state)
            self._focus_window(selected_window)
            time.sleep(0.2)
            return {
                "action": "computer.show_app",
                "shown": True,
                "platform": platform.system(),
                "target_window": selected_window,
                "active_window": self._active_window(),
            }
        result = self._select_app(action_payload)
        shown = bool(result.get("selected"))
        active_window = None
        if shown:
            time.sleep(0.2)
            target_app = result.get("target_app") if isinstance(result.get("target_app"), dict) else {}
            active_window = self._active_window_for_app(str(target_app.get("name") or target_app.get("app") or ""))
            if active_window is not None:
                state = self._computer_state()
                state["target_window"] = active_window
                self._write_computer_state(state)
        return {
            "action": "computer.show_app",
            "shown": shown,
            "platform": platform.system(),
            **({"target_app": result.get("target_app")} if result.get("target_app") else {}),
            **({"target_window": active_window} if active_window else {}),
            **({"open_apps": result.get("open_apps")} if result.get("open_apps") else {}),
            **({"installed_match": result.get("installed_match")} if result.get("installed_match") else {}),
            "active_window": active_window or (self._active_window() if shown else None),
            **({"reason": "No running or installed app matched the request."} if not shown else {}),
        }

    @classmethod
    def _app_alias_tokens(cls, value: Any) -> set[str]:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return set()

        variants: set[str] = {normalized}
        collapsed = re.sub(r"[^a-z0-9]+", "", normalized)
        spaced = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
        if collapsed:
            variants.add(collapsed)
        if spaced:
            variants.add(spaced)
        if normalized.endswith(".exe"):
            base = normalized[:-4].strip()
            if base:
                variants.add(base)
                variants.add(re.sub(r"[^a-z0-9]+", "", base))
                variants.add(re.sub(r"[^a-z0-9]+", " ", base).strip())

        alias_groups = (
            {"chatgpt atlas", "chatgptatlas", "atlas", "openai atlas", "openaiatlas"},
            {"google chrome", "googlechrome", "chrome", "chrome.exe"},
            {"microsoft edge", "microsoftedge", "ms edge", "edge", "msedge", "msedge.exe"},
            {"mozilla firefox", "mozillafirefox", "firefox", "firefox.exe"},
        )
        for group in alias_groups:
            if variants & group:
                variants.update(group)
        return {item for item in variants if item}

    @classmethod
    def _app_name_matches(cls, needle: str, haystack: str) -> bool:
        need = cls._app_alias_tokens(needle)
        if not need:
            return True
        hay = cls._app_alias_tokens(haystack)
        if not hay:
            return False
        if need & hay:
            return True
        return any(left in right for left in need for right in hay)

    @classmethod
    def _app_matches_filter(cls, app: dict[str, Any], needle: str) -> bool:
        value = needle.strip()
        if not value:
            return True
        haystacks = [
            str(app.get("name") or ""),
            str(app.get("app") or ""),
            str(app.get("bundle_id") or ""),
            str(app.get("path") or ""),
            str(app.get("title") or ""),
        ]
        return any(cls._app_name_matches(value, item) for item in haystacks if item)

    @staticmethod
    def _normalize_app_record(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        name = str(value.get("name") or value.get("app") or value.get("process") or "").strip()
        record: dict[str, Any] = {"name": name, "app": name}
        for key in ("pid", "bundle_id", "path", "title", "source"):
            if value.get(key) not in (None, ""):
                record[key] = value.get(key)
        for key in ("active", "running", "has_windows"):
            if key in value:
                record[key] = bool(value.get(key))
        if value.get("window_count") is not None:
            try:
                record["window_count"] = int(value.get("window_count") or 0)
            except Exception:
                pass
        return record

    @staticmethod
    def _image_data_url(path: Path) -> str:
        try:
            mime_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            return "data:{};base64,".format(mime_type) + base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception:
            return ""

    def _model_screenshot_copy(self, path: Path) -> Path:
        preview_path = path.with_name(path.stem + "-model.png")
        if platform.system() == "Darwin":
            try:
                subprocess.run(
                    ["sips", "-Z", "640", "-s", "format", "png", str(path), "--out", str(preview_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if preview_path.exists() and preview_path.stat().st_size > 0:
                    return preview_path
            except Exception:
                pass
        try:
            shutil.copyfile(path, preview_path)
            if preview_path.exists() and preview_path.stat().st_size > 0:
                return preview_path
        except Exception:
            pass
        return path

    @staticmethod
    def _image_size(path: Path) -> tuple[int, int] | None:
        try:
            data = path.read_bytes()
        except Exception:
            return None
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            try:
                width, height = struct.unpack(">II", data[16:24])
                return int(width), int(height)
            except Exception:
                return None
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                index += 2
                if marker in {0xD8, 0xD9}:
                    continue
                if index + 2 > len(data):
                    return None
                length = int.from_bytes(data[index : index + 2], "big")
                if length < 2 or index + length > len(data):
                    return None
                if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC} and length >= 7:
                    height = int.from_bytes(data[index + 3 : index + 5], "big")
                    width = int.from_bytes(data[index + 5 : index + 7], "big")
                    return int(width), int(height)
                index += length
        return None

    def _marker_preview_image(
        self,
        model_path: Path,
        screenshot_result: dict[str, Any],
        *,
        marker: dict[str, Any] | None = None,
        drag_marker: dict[str, Any] | None = None,
    ) -> Path | None:
        points: list[tuple[int, int]] = []
        for item in self._marker_items(marker, drag_marker):
            point = self._marker_to_model_point(item, screenshot_result)
            if point is not None:
                points.append(point)
        if not points:
            return None
        marked_path = model_path.with_name(model_path.stem + "-marked.png")
        if self._annotate_png_with_markers(model_path, marked_path, points):
            return marked_path
        return None

    @staticmethod
    def _marker_items(marker: dict[str, Any] | None, drag_marker: dict[str, Any] | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(marker, dict):
            items.append(marker)
        if isinstance(drag_marker, dict):
            for key in ("from", "to"):
                value = drag_marker.get(key)
                if isinstance(value, dict):
                    items.append(value)
        return items

    def _marker_to_model_point(self, marker: dict[str, Any], result: dict[str, Any]) -> tuple[int, int] | None:
        model_size = result.get("model_image_size") if isinstance(result.get("model_image_size"), dict) else {}
        try:
            model_width = int(model_size.get("width", 0))
            model_height = int(model_size.get("height", 0))
        except Exception:
            return None
        if model_width <= 0 or model_height <= 0:
            return None
        space = str(marker.get("coordinate_space") or "").strip().lower()
        if space in self._normalized_coordinate_spaces():
            x = self._normalized_to_pixel(marker.get("normalized_x", marker.get("x", 0)), model_width)
            y = self._normalized_to_pixel(marker.get("normalized_y", marker.get("y", 0)), model_height)
            return x, y
        if space in {"model", "model_image", "preview", "screenshot_preview"}:
            return self._clamped_model_point(marker.get("x", 0), marker.get("y", 0), model_width, model_height)
        image_size = result.get("image_size") if isinstance(result.get("image_size"), dict) else {}
        if space in {"screenshot", "screenshot_image", "image", "window", "target"}:
            try:
                image_width = int(image_size.get("width", 0))
                image_height = int(image_size.get("height", 0))
            except Exception:
                return None
            if image_width <= 0 or image_height <= 0:
                return None
            x = round(self._numeric_coordinate(marker.get("x", 0)) * max(model_width - 1, 0) / max(image_width - 1, 1))
            y = round(self._numeric_coordinate(marker.get("y", 0)) * max(model_height - 1, 0) / max(image_height - 1, 1))
            return self._clamped_model_point(x, y, model_width, model_height)
        action_space = result.get("action_coordinate_system") if isinstance(result.get("action_coordinate_system"), dict) else {}
        try:
            action_x = int(action_space.get("x", 0))
            action_y = int(action_space.get("y", 0))
            action_width = int(action_space.get("width", 0))
            action_height = int(action_space.get("height", 0))
        except Exception:
            return None
        if action_width <= 0 or action_height <= 0:
            return None
        screen_x = self._numeric_coordinate(marker.get("screen_x", marker.get("x", 0)))
        screen_y = self._numeric_coordinate(marker.get("screen_y", marker.get("y", 0)))
        x = round((screen_x - action_x) * max(model_width - 1, 0) / max(action_width - 1, 1))
        y = round((screen_y - action_y) * max(model_height - 1, 0) / max(action_height - 1, 1))
        return self._clamped_model_point(x, y, model_width, model_height)

    @staticmethod
    def _clamped_model_point(x: Any, y: Any, width: int, height: int) -> tuple[int, int]:
        try:
            px = int(round(float(x)))
            py = int(round(float(y)))
        except Exception:
            px = 0
            py = 0
        return max(0, min(px, width - 1)), max(0, min(py, height - 1))

    def _annotate_png_with_markers(self, source_path: Path, output_path: Path, points: list[tuple[int, int]]) -> bool:
        image = self._read_png_rgba(source_path)
        if image is None:
            return False
        width, height, pixels = image
        for x, y in points:
            self._draw_marker(pixels, width, height, x, y)
        return self._write_png_rgba(output_path, width, height, pixels)

    @staticmethod
    def _draw_marker(pixels: bytearray, width: int, height: int, x: int, y: int) -> None:
        radius = max(8, min(width, height) // 40)
        thickness = max(2, radius // 5)

        def set_pixel(px: int, py: int, color: tuple[int, int, int, int]) -> None:
            if 0 <= px < width and 0 <= py < height:
                index = (py * width + px) * 4
                pixels[index : index + 4] = bytes(color)

        red = (255, 0, 0, 255)
        for offset in range(-radius, radius + 1):
            for thick in range(-thickness, thickness + 1):
                set_pixel(x + offset, y + thick, red)
                set_pixel(x + thick, y + offset, red)
        inner = max(radius - thickness, 1)
        outer = radius + thickness
        inner_sq = inner * inner
        outer_sq = outer * outer
        for py in range(y - outer, y + outer + 1):
            for px in range(x - outer, x + outer + 1):
                distance_sq = (px - x) * (px - x) + (py - y) * (py - y)
                if inner_sq <= distance_sq <= outer_sq:
                    set_pixel(px, py, red)

    @staticmethod
    def _read_png_rgba(path: Path) -> tuple[int, int, bytearray] | None:
        try:
            data = path.read_bytes()
        except Exception:
            return None
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        offset = 8
        width = height = bit_depth = color_type = interlace = 0
        idat = bytearray()
        while offset + 8 <= len(data):
            length = int.from_bytes(data[offset : offset + 4], "big")
            chunk_type = data[offset + 4 : offset + 8]
            chunk_data = data[offset + 8 : offset + 8 + length]
            offset += 12 + length
            if chunk_type == b"IHDR":
                if len(chunk_data) < 13:
                    return None
                width, height = struct.unpack(">II", chunk_data[:8])
                bit_depth = chunk_data[8]
                color_type = chunk_data[9]
                interlace = chunk_data[12]
            elif chunk_type == b"IDAT":
                idat.extend(chunk_data)
            elif chunk_type == b"IEND":
                break
        if width <= 0 or height <= 0 or bit_depth != 8 or color_type not in {2, 6} or interlace != 0:
            return None
        channels = 4 if color_type == 6 else 3
        stride = width * channels
        try:
            raw = zlib.decompress(bytes(idat))
        except Exception:
            return None
        rows: list[bytearray] = []
        cursor = 0
        previous = bytearray(stride)
        for _ in range(height):
            if cursor >= len(raw):
                return None
            filter_type = raw[cursor]
            cursor += 1
            scanline = bytearray(raw[cursor : cursor + stride])
            cursor += stride
            if len(scanline) != stride:
                return None
            BrowserComputerController._unfilter_png_scanline(scanline, previous, filter_type, channels)
            rows.append(scanline)
            previous = scanline
        pixels = bytearray(width * height * 4)
        for row_index, row in enumerate(rows):
            for column in range(width):
                src = column * channels
                dest = (row_index * width + column) * 4
                pixels[dest] = row[src]
                pixels[dest + 1] = row[src + 1]
                pixels[dest + 2] = row[src + 2]
                pixels[dest + 3] = row[src + 3] if channels == 4 else 255
        return int(width), int(height), pixels

    @staticmethod
    def _unfilter_png_scanline(scanline: bytearray, previous: bytearray, filter_type: int, bpp: int) -> None:
        if filter_type == 0:
            return
        for index in range(len(scanline)):
            left = scanline[index - bpp] if index >= bpp else 0
            up = previous[index] if index < len(previous) else 0
            up_left = previous[index - bpp] if index >= bpp and index - bpp < len(previous) else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                predictor = BrowserComputerController._png_paeth(left, up, up_left)
            else:
                predictor = 0
            scanline[index] = (scanline[index] + predictor) & 0xFF

    @staticmethod
    def _png_paeth(left: int, up: int, up_left: int) -> int:
        estimate = left + up - up_left
        pa = abs(estimate - left)
        pb = abs(estimate - up)
        pc = abs(estimate - up_left)
        if pa <= pb and pa <= pc:
            return left
        if pb <= pc:
            return up
        return up_left

    @staticmethod
    def _write_png_rgba(path: Path, width: int, height: int, pixels: bytearray) -> bool:
        def chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
            return (
                len(chunk_data).to_bytes(4, "big")
                + chunk_type
                + chunk_data
                + zlib.crc32(chunk_type + chunk_data).to_bytes(4, "big")
            )

        try:
            rows = bytearray()
            stride = width * 4
            for row in range(height):
                rows.append(0)
                start = row * stride
                rows.extend(pixels[start : start + stride])
            ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))
            return True
        except Exception:
            return False

    def _crop_png(self, source_path: Path, output_path: Path, box: tuple[int, int, int, int]) -> bool:
        image = self._read_png_rgba(source_path)
        if image is None:
            return False
        width, _height, pixels = image
        left, top, right, bottom = box
        crop_width = right - left + 1
        crop_height = bottom - top + 1
        cropped = bytearray(crop_width * crop_height * 4)
        for row in range(crop_height):
            src_start = ((top + row) * width + left) * 4
            src_end = src_start + crop_width * 4
            dest_start = row * crop_width * 4
            cropped[dest_start : dest_start + crop_width * 4] = pixels[src_start:src_end]
        return self._write_png_rgba(output_path, crop_width, crop_height, cropped)

    @staticmethod
    def _cursor_position() -> dict[str, Any] | None:
        system = platform.system()
        try:
            if system == "Darwin":
                code = (
                    "import json, Quartz\n"
                    "event = Quartz.CGEventCreate(None)\n"
                    "loc = Quartz.CGEventGetLocation(event)\n"
                    "print(json.dumps({'x': int(round(loc.x)), 'y': int(round(loc.y)), 'origin': 'top_left'}))"
                )
                completed = subprocess.run(
                    _current_python_snippet_command(code),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
                )
                value = json.loads(completed.stdout or "{}")
                if "x" in value and "y" in value:
                    return value
            if system == "Windows":
                script = "\n".join(
                    [
                        "Add-Type -AssemblyName System.Windows.Forms",
                        BrowserComputerController._windows_dpi_awareness_script(),
                        "$p = [System.Windows.Forms.Cursor]::Position",
                        "ConvertTo-Json @{ x = [int]$p.X; y = [int]$p.Y; origin = 'top_left' } -Compress",
                    ]
                )
                executable = "powershell" if shutil.which("powershell") else "pwsh"
                completed = subprocess.run([executable, "-NoProfile", "-Command", script], check=True, capture_output=True, text=True)
                value = json.loads(completed.stdout or "{}")
                if "x" in value and "y" in value:
                    return value
        except Exception:
            return None
        return None

    @staticmethod
    def _action_coordinate_system(
        system: str,
        image_size: tuple[int, int] | None,
        *,
        capture_target: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if capture_target and capture_target.get("width") and capture_target.get("height"):
            x = int(capture_target.get("x", 0))
            y = int(capture_target.get("y", 0))
            width = int(capture_target.get("width", 0))
            height = int(capture_target.get("height", 0))
            return {
                "origin": "top_left",
                "unit": capture_target.get("unit") or "display_coordinate",
                "screen": capture_target.get("screen") or "selected_window",
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "x_range": [x, x + max(width - 1, 0)],
                "y_range": [y, y + max(height - 1, 0)],
            }
        if system == "Darwin":
            try:
                code = (
                    "import json, Quartz\n"
                    "display = Quartz.CGMainDisplayID()\n"
                    "bounds = Quartz.CGDisplayBounds(display)\n"
                    "payload = {\n"
                    "  'origin': 'top_left',\n"
                    "  'unit': 'display_coordinate',\n"
                    "  'screen': 'primary',\n"
                    "  'x': int(round(bounds.origin.x)),\n"
                    "  'y': int(round(bounds.origin.y)),\n"
                    "  'width': int(round(bounds.size.width)),\n"
                    "  'height': int(round(bounds.size.height)),\n"
                    "}\n"
                    "payload['x_range'] = [payload['x'], payload['x'] + max(payload['width'] - 1, 0)]\n"
                    "payload['y_range'] = [payload['y'], payload['y'] + max(payload['height'] - 1, 0)]\n"
                    "print(json.dumps(payload))"
                )
                completed = subprocess.run(
                    _current_python_snippet_command(code),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
                )
                value = json.loads(completed.stdout or "{}")
                if value.get("width") and value.get("height"):
                    return value
            except Exception:
                pass
        if system == "Windows" and image_size:
            width, height = image_size
            return {
                "origin": "top_left",
                "unit": "px",
                "screen": "primary",
                "x": 0,
                "y": 0,
                "width": width,
                "height": height,
                "x_range": [0, max(width - 1, 0)],
                "y_range": [0, max(height - 1, 0)],
            }
        if image_size:
            width, height = image_size
            return {
                "origin": "top_left",
                "unit": "px",
                "screen": "captured",
                "x": 0,
                "y": 0,
                "width": width,
                "height": height,
                "x_range": [0, max(width - 1, 0)],
                "y_range": [0, max(height - 1, 0)],
            }
        return None

    # ------------------------------------------------------------------
    # ComputerSeat delegation methods
    # ------------------------------------------------------------------

    def _computer_seat_target(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Build a ComputerTarget dict from payload."""
        coordinate_space = str(payload.get("coordinate_space") or payload.get("space") or "window").strip() or "window"
        has_explicit_target = self._has_explicit_window_filter(payload)
        window = self._matching_window(payload) if has_explicit_target else None
        state = self._computer_state()
        if not self._state_matches_artifact_root(state):
            state = {}
        if window is None and not has_explicit_target:
            selected = state.get("target_window")
            window = self._normalize_window_record(selected) if isinstance(selected, dict) else None
        selected_app = state.get("target_app") if (not has_explicit_target and isinstance(state.get("target_app"), dict)) else {}
        app = payload.get("app") or payload.get("application") or (window or {}).get("app") or selected_app.get("app") or selected_app.get("name")
        pid = payload.get("pid") or (window or {}).get("pid") or selected_app.get("pid")
        resolution_error = ""
        if has_explicit_target and window is None and not self._explicit_target_has_self_contained_window(payload):
            resolution_error = "No visible window matched the explicit target; refusing to reuse a previously selected window."
            pid = None
        if pid and app and not self._pid_matches_app(pid, str(app)):
            resolution_error = "Explicit PID does not match the requested app."
            pid = None
        if not pid and not resolution_error:
            pid = self._pid_for_app_name(str(app or ""))
        target = {
            "kind": payload.get("target_kind") or payload.get("kind") or "desktop",
            "app": app,
            "pid": pid,
            "window_id": payload.get("window_id") or (window or {}).get("window_id"),
            "window_title": payload.get("title") or payload.get("window_title") or (window or {}).get("title"),
            "hwnd": payload.get("hwnd"),
            "bundle_id": payload.get("bundle_id"),
            "browser_client_id": payload.get("browser_client_id") or payload.get("client_id"),
            "browser_tab_id": payload.get("browser_tab_id") or payload.get("tab_id"),
            "url": payload.get("url"),
            "coordinate_space": coordinate_space,
            "surface_id": payload.get("surface_id"),
            "observation_revision": payload.get("observation_revision"),
        }
        if isinstance(window, dict):
            target["window_bounds"] = {
                key: window[key]
                for key in ("x", "y", "width", "height")
                if window.get(key) is not None
            }
        if resolution_error:
            target["_target_resolution_error"] = resolution_error
        return target

    def _pid_for_app_name(self, app_name: str) -> int | None:
        app_name = str(app_name or "").strip()
        if not app_name:
            return None
        matches: list[dict[str, Any]] = []
        for item in self._running_apps():
            if isinstance(item, dict) and self._app_matches_filter(item, app_name):
                matches.append(item)
        if not matches:
            return None

        needle = app_name.lower()

        def score(item: dict[str, Any]) -> tuple[int, int]:
            names = [str(item.get("name") or "").lower(), str(item.get("app") or "").lower()]
            exact = 1 if needle in names else 0
            helper_penalty = 1 if any("helper" in name for name in names) else 0
            return (exact, -helper_penalty)

        selected = max(matches, key=score)
        try:
            pid = int(selected.get("pid") or 0)
        except Exception:
            return None
        return pid if pid > 0 else None

    def _pid_matches_app(self, pid: Any, app_name: str) -> bool:
        try:
            target_pid = int(pid)
        except Exception:
            return False
        if target_pid <= 0:
            return False
        app_name = str(app_name or "").strip()
        if not app_name:
            return True
        for item in self._running_apps():
            if not isinstance(item, dict):
                continue
            try:
                item_pid = int(item.get("pid") or 0)
            except Exception:
                continue
            if item_pid == target_pid:
                return self._app_matches_filter(item, app_name)
        for item in self._list_windows():
            window = self._normalize_window_record(item)
            if not window:
                continue
            if int(window.get("pid") or 0) == target_pid:
                return self._window_matches_filter(window, app=app_name.lower())
        return False

    def _computer_seat_metadata_for_target(self, target_record: dict[str, Any] | None) -> dict[str, Any]:
        """Build additive ComputerSeat metadata for a selected target."""
        meta: dict[str, Any] = {}
        try:
            svc = self._get_computer_seat()
            doctor = svc.doctor()
            meta["driver_chain_order"] = doctor.get("driver_chain_order", [])
            meta["capabilities"] = [d.get("capabilities", {}) for d in doctor.get("available_drivers", [])]
        except Exception:
            pass
        if target_record:
            meta["target"] = {
                "app": target_record.get("app") or target_record.get("name"),
                "pid": target_record.get("pid"),
                "window_id": target_record.get("window_id") or target_record.get("id"),
                "window_title": target_record.get("title"),
            }
            meta["recommended_next_actions"] = ["computer.screenshot", "computer.click", "computer.observe"]
            self._with_browser_text_input_recommendations(meta)
        return meta

    def _computer_seat_observe(self, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        """Delegate to ComputerSeatService.observe with approval.

        observe can aggregate screenshot-capable and foreground drivers, so it
        must use the same explicit approval boundary as computer.screenshot.
        """
        approval_payload = self._safe_payload(payload)
        if not (yolo_mode or self._consume_approval(payload, "computer.observe", approval_payload)):
            return self._approval_required("computer.observe", approval_payload)
        try:
            svc = self._get_computer_seat()
            target = self._computer_seat_target(payload)
            result = svc.observe(target)
            result["action"] = "computer.observe"
            if isinstance(result, dict):
                self._with_browser_text_input_recommendations(result)
            return result
        except Exception as e:
            return {"action": "computer.observe", "error": str(e)}

    def _computer_read_action(self, action: str, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        """Run high-risk read actions through Swift host first, then ComputerSeat fallback."""
        approval_payload = self._safe_payload(payload)
        if not (yolo_mode or self._consume_approval(payload, action, approval_payload)):
            return self._approval_required(action, approval_payload)
        swift_result = self._darwin_swift_optional_action_result(action, payload)
        if swift_result is not None:
            swift_result.setdefault("action", action)
            return swift_result
        if action == "computer.ax_tree":
            return self._computer_seat_ax_tree(payload)
        if action == "computer.ocr":
            return self._computer_seat_ocr(payload)
        return self._unsupported_computer_action(action, payload, reason="Unsupported computer read action.")

    def _computer_seat_ax_tree(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            svc = self._get_computer_seat()
            result = svc.observe(self._computer_seat_target(payload))
        except Exception as e:
            return {"action": "computer.ax_tree", "supported": False, "is_error": True, "reason": str(e)}
        ax_tree = result.get("ax_tree") if isinstance(result.get("ax_tree"), dict) else {}
        response: dict[str, Any] = {
            "action": "computer.ax_tree",
            "platform": result.get("platform", platform.system()),
            "supported": bool(ax_tree),
            "ax_tree": ax_tree,
        }
        self._copy_optional_keys(
            result,
            response,
            ("target_window", "capabilities", "recommended_next_actions", "fallback_available"),
        )
        if self._truthy(payload.get("include_screenshot")) and isinstance(result.get("screenshot"), dict):
            response["screenshot"] = result.get("screenshot")
        if self._truthy(payload.get("include_ocr")):
            ocr_payload = self._ocr_payload_from_observe(result)
            if ocr_payload:
                response["ocr"] = ocr_payload
        if not ax_tree:
            response["reason"] = "No accessibility tree is available from the current computer drivers."
            response["recovery"] = {
                "kind": "driver_not_supported",
                "note": "Try computer.observe or use a host/driver with accessibility tree support.",
            }
        return response

    def _computer_seat_ocr(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            svc = self._get_computer_seat()
            result = svc.observe(self._computer_seat_target(payload))
        except Exception as e:
            return {"action": "computer.ocr", "supported": False, "is_error": True, "reason": str(e)}
        ocr_payload = self._ocr_payload_from_observe(result)
        response: dict[str, Any] = {
            "action": "computer.ocr",
            "platform": result.get("platform", platform.system()),
            "supported": bool(ocr_payload),
        }
        if ocr_payload:
            response.update(ocr_payload)
        self._copy_optional_keys(result, response, ("target_window", "capabilities", "fallback_available"))
        if self._truthy(payload.get("include_ax_tree")) and isinstance(result.get("ax_tree"), dict):
            response["ax_tree"] = result.get("ax_tree")
        if self._truthy(payload.get("include_screenshot")) and isinstance(result.get("screenshot"), dict):
            response["screenshot"] = result.get("screenshot")
        if not ocr_payload:
            response["reason"] = "No OCR-capable computer host or fallback driver is available for this target."
            response["recovery"] = {
                "kind": "driver_not_supported",
                "note": "Use computer.screenshot for visual inspection, or enable a host/driver that exposes OCR.",
            }
        return response

    @staticmethod
    def _ocr_payload_from_observe(result: dict[str, Any]) -> dict[str, Any]:
        for key in ("ocr", "ocr_result"):
            value = result.get(key)
            if isinstance(value, dict) and value:
                return dict(value)
        text = str(result.get("ocr_text") or "").strip()
        if text:
            return {"text": text, "ocr_text": text}
        items = result.get("ocr_items")
        if isinstance(items, list) and items:
            return {"items": items}
        screenshot = result.get("screenshot")
        if isinstance(screenshot, dict):
            for key in ("ocr", "ocr_result"):
                value = screenshot.get(key)
                if isinstance(value, dict) and value:
                    return dict(value)
            text = str(screenshot.get("ocr_text") or "").strip()
            if text:
                return {"text": text, "ocr_text": text}
            items = screenshot.get("ocr_items")
            if isinstance(items, list) and items:
                return {"items": items}
        return {}

    @staticmethod
    def _copy_optional_keys(source: dict[str, Any], target: dict[str, Any], keys: tuple[str, ...]) -> None:
        for key in keys:
            value = source.get(key)
            if value not in (None, {}, []):
                target[key] = value

    def _computer_click_text(self, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        approval_payload = self._safe_payload(payload)
        if not (yolo_mode or self._consume_approval(payload, "computer.click_text", approval_payload)):
            return self._approval_required("computer.click_text", approval_payload)
        swift_result = self._darwin_swift_optional_action_result(
            "computer.click_text",
            self._click_text_swift_payload(payload),
        )
        if swift_result is not None:
            swift_result.setdefault("action", "computer.click_text")
            return swift_result
        text_query = self._text_query_from_payload(payload)
        if not text_query and not str(payload.get("element_id") or "").strip():
            return {
                "action": "computer.click_text",
                "executed": False,
                "supported": False,
                "is_error": True,
                "reason": "computer.click_text requires text, query, text_query, match_text, or element_id.",
            }
        try:
            svc = self._get_computer_seat()
            target = self._computer_seat_target(payload)
            element_or_point = self._click_text_element_or_point(payload, text_query)
            intent = self._click_text_intent(payload, text_query)
            with self._edge_haze("computer.click_text", payload):
                result = svc.semantic_action(target, intent=intent, element_or_point=element_or_point)
            result["action"] = "computer.click_text"
            result.setdefault("underlying_action", "computer.semantic_action")
            if text_query:
                result.setdefault("text_query", text_query)
            if not result.get("executed"):
                result.setdefault("supported", False)
                result.setdefault("reason", "No text-click capable host or semantic fallback driver accepted the request.")
            return result
        except Exception as e:
            return {"action": "computer.click_text", "supported": False, "is_error": True, "reason": str(e)}

    @classmethod
    def _text_query_from_payload(cls, payload: dict[str, Any]) -> str:
        for key in ("text", "query", "text_query", "match_text"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _click_text_swift_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("text") or "").strip():
            return payload
        for key in ("text_query", "match_text"):
            value = str(payload.get(key) or "").strip()
            if value:
                swift_payload = dict(payload)
                swift_payload["text"] = value
                return swift_payload
        return payload

    @classmethod
    def _click_text_intent(cls, payload: dict[str, Any], text_query: str) -> str:
        explicit = str(payload.get("intent") or "").strip()
        if explicit:
            return explicit
        role = str(payload.get("role") or "").strip()
        if text_query and role:
            return f"click the {role} matching text: {text_query}"
        if text_query:
            return f"click text: {text_query}"
        element_id = str(payload.get("element_id") or "").strip()
        return f"click accessibility element: {element_id}"

    @classmethod
    def _click_text_element_or_point(cls, payload: dict[str, Any], text_query: str) -> dict[str, Any]:
        element: dict[str, Any] = {}
        for key in ("element_id", "role", "confidence_threshold"):
            value = payload.get(key)
            if value not in (None, ""):
                output_key = "id" if key == "element_id" else key
                element[output_key] = value
        if text_query:
            element["text"] = text_query
        for key in ("query", "text_query", "match_text"):
            value = payload.get(key)
            if value not in (None, ""):
                element[key] = value
        return element

    def _unsupported_computer_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        reason: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": action,
            "supported": False,
            "platform": platform.system(),
            "reason": reason,
            "recovery": {
                "kind": "driver_not_supported",
                "note": "Use computer.observe/screenshot or enable a host driver that supports this action.",
            },
        }
        if payload:
            result["payload"] = self._safe_payload(payload)
        return result

    def _computer_seat_semantic_action(self, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        """Delegate to ComputerSeatService.semantic_action with approval."""
        if not yolo_mode and not self._consume_approval(payload, "computer.semantic_action", self._safe_payload(payload)):
            return self._approval_required("computer.semantic_action", self._safe_payload(payload))
        try:
            svc = self._get_computer_seat()
            target = self._computer_seat_target(payload)
            element_or_point = None
            if payload.get("element_id"):
                element_or_point = {"id": payload["element_id"]}
            elif payload.get("point"):
                element_or_point = tuple(payload["point"])
            with self._edge_haze("computer.semantic_action", payload):
                result = svc.semantic_action(target, intent=payload.get("intent", ""), element_or_point=element_or_point)
            result["action"] = "computer.semantic_action"
            return result
        except Exception as e:
            return {"action": "computer.semantic_action", "error": str(e)}

    def _computer_seat_pid_event(self, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        """Delegate to ComputerSeatService for pid-targeted events."""
        if not yolo_mode and not self._consume_approval(payload, "computer.pid_event", self._safe_payload(payload)):
            return self._approval_required("computer.pid_event", self._safe_payload(payload))
        try:
            svc = self._get_computer_seat()
            target = self._pid_event_target(payload)
            action = self._pid_event_sub_action(payload)
            with self._edge_haze("computer.pid_event", payload):
                result = svc.pid_event(action, target, self._pid_event_payload(action, payload))
            result["action"] = "computer.pid_event"
            result["sub_action"] = action
            result["_experimental"] = True
            return result
        except Exception as e:
            return {"action": "computer.pid_event", "error": str(e), "_experimental": True}

    @staticmethod
    def _pid_event_sub_action(payload: dict[str, Any]) -> str:
        raw = str(payload.get("sub_action") or payload.get("action_type") or payload.get("action") or "click").strip()
        raw = raw.removeprefix("computer.")
        aliases = {
            "type": "type_text",
            "type_text": "type_text",
            "text": "type_text",
            "click": "click",
            "key": "key",
            "scroll": "scroll",
        }
        if raw not in aliases:
            raise ValueError(f"Unknown pid_event sub-action: {raw}")
        return aliases[raw]

    @staticmethod
    def _pid_event_target(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": payload.get("target_kind") or payload.get("kind") or "desktop",
            "app": payload.get("app") or payload.get("application"),
            "pid": payload.get("pid"),
            "window_id": payload.get("window_id"),
            "window_title": payload.get("title") or payload.get("window_title"),
            "hwnd": payload.get("hwnd"),
            "bundle_id": payload.get("bundle_id"),
            "coordinate_space": payload.get("coordinate_space") or payload.get("space") or "window",
        }

    @staticmethod
    def _pid_event_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "click":
            return {"x": payload.get("x", 0), "y": payload.get("y", 0), "button": payload.get("button", "left")}
        if action == "type_text":
            return {"text": payload.get("text", "")}
        if action == "key":
            return {"key_combo": payload.get("key_combo") or _key_combo_from_payload(payload)}
        if action == "scroll":
            return {
                "x": payload.get("x", 0),
                "y": payload.get("y", 0),
                "direction": payload.get("direction", "down"),
                "clicks": payload.get("clicks", payload.get("amount", 3)),
            }
        raise ValueError(f"Unknown pid_event sub-action: {action}")

    def _computer_seat_doctor(self) -> dict[str, Any]:
        """Delegate to ComputerSeatService.doctor."""
        try:
            svc = self._get_computer_seat()
            result = svc.doctor()
            result["action"] = "computer.doctor"
            return result
        except Exception as e:
            return {"action": "computer.doctor", "error": str(e)}

    def _computer_seat_screenshot_compat(self, payload: dict[str, Any], *, dry_run: bool, yolo_mode: bool) -> dict[str, Any] | None:
        """Try ComputerSeatService.observe for screenshot, return legacy schema or None on failure."""
        try:
            svc = self._get_computer_seat()
            target = self._computer_seat_target(payload)
            svc.observe(target)
            # If observe returned a screenshot, we could use it – but for now
            # we only add metadata. The legacy _screenshot path handles the
            # actual capture with all its crop/model logic.
            return None
        except Exception:
            return None

    def _try_computer_seat_action(
        self,
        action: str,
        action_payload: dict[str, Any],
        *,
        background_safe_only: bool = False,
        verified_background_only: bool = False,
    ) -> dict[str, Any] | None:
        """Attempt to execute a mutation action via ComputerSeatService.

        Returns the ActionResult dict if ComputerSeat executed it. For explicit
        physical pointer actions, foreground fallback still falls through to
        legacy platform code because it owns the visible click/drag path.
        """
        if (
            action in {"computer.move", "computer.click", "computer.drag"}
            and action_payload.get("physical") is True
            and platform.system() == "Darwin"
            and not _running_under_pytest()
        ):
            return None
        try:
            svc = self._get_computer_seat()
        except Exception:
            return None

        target = self._computer_seat_target(action_payload)
        target_error = str(target.get("_target_resolution_error") or "")
        if target_error:
            return {
                "action": action,
                "driver": "computer_seat",
                "executed": False,
                "confidence": "failed",
                "is_error": True,
                "notes": [target_error],
            }
        try:
            if background_safe_only:
                background_method = getattr(svc, "background_action", None)
                if not callable(background_method):
                    return {
                        "action": action,
                        "driver": "computer_seat",
                        "executed": False,
                        "confidence": "failed",
                        "is_error": True,
                        "notes": ["ComputerSeatService does not expose a background_action API."],
                    }
                service_action = {
                    "computer.click": "click",
                    "computer.type": "type_text",
                    "computer.key": "key",
                    "computer.scroll": "scroll",
                }.get(action)
                if service_action is None:
                    return None
                if service_action == "type_text":
                    service_payload = {"text": action_payload.get("text", "")}
                elif service_action == "key":
                    service_payload = {"key_combo": _key_combo_from_payload(action_payload)}
                elif service_action == "scroll":
                    service_payload = {
                        "x": int(action_payload.get("x", 0)),
                        "y": int(action_payload.get("y", 0)),
                        "direction": action_payload.get("direction", "down"),
                        "clicks": int(action_payload.get("amount", 3)),
                    }
                else:
                    service_payload = {
                        "x": int(action_payload.get("x", 0)),
                        "y": int(action_payload.get("y", 0)),
                        "button": action_payload.get("button", "left"),
                    }
                if service_action == "key":
                    background_result: dict[str, Any] = {}
                    last_success: dict[str, Any] = {}
                    count = _key_press_count(action_payload)
                    executed_count = 0
                    for _ in range(count):
                        background_result = background_method(
                            service_action,
                            target,
                            service_payload,
                            verified_only=verified_background_only,
                        )
                        if not background_result or not background_result.get("executed"):
                            break
                        executed_count += 1
                        last_success = dict(background_result)
                    if executed_count > 0:
                        background_result = dict(last_success)
                        background_result["executed"] = True
                        background_result["executed_count"] = executed_count
                        background_result["requested_count"] = count
                        background_result["count"] = executed_count
                        if executed_count < count:
                            background_result["partial_success"] = True
                            background_result.setdefault("notes", [])
                            if isinstance(background_result["notes"], list):
                                background_result["notes"].append(
                                    f"Stopped after {executed_count} of {count} background key press(es); refusing foreground replay."
                                )
                    elif background_result:
                        background_result["executed_count"] = 0
                        background_result["requested_count"] = count
                    if background_result:
                        background_result.setdefault("count", executed_count)
                        background_result.setdefault("requested_count", count)
                else:
                    background_result = background_method(
                        service_action,
                        target,
                        service_payload,
                        verified_only=verified_background_only,
                    )
                return background_result if isinstance(background_result, dict) else None
            if action == "computer.click":
                result = svc.click(target, x=int(action_payload.get("x", 0)), y=int(action_payload.get("y", 0)), button=action_payload.get("button", "left"))
            elif action == "computer.type":
                result = svc.type_text(target, text=action_payload.get("text", ""))
            elif action == "computer.key":
                count = _key_press_count(action_payload)
                result = {}
                for _ in range(count):
                    result = svc.key(target, key_combo=_key_combo_from_payload(action_payload))
                    if not result or not result.get("executed"):
                        break
                if result:
                    result["count"] = count
                    result["requested_count"] = count
            elif action == "computer.scroll":
                direction = action_payload.get("direction", "down")
                result = svc.scroll(target, x=int(action_payload.get("x", 0)), y=int(action_payload.get("y", 0)), direction=direction, clicks=int(action_payload.get("amount", 3)))
            elif action == "computer.move":
                result = svc.move(target, x=int(action_payload.get("x", 0)), y=int(action_payload.get("y", 0)))
            elif action == "computer.drag":
                result = svc.drag(target, x1=int(action_payload.get("x1", 0)), y1=int(action_payload.get("y1", 0)), x2=int(action_payload.get("x2", 0)), y2=int(action_payload.get("y2", 0)))
            else:
                return None
            if result and result.get("executed"):
                if (
                    result.get("is_fallback")
                    and action in {"computer.click", "computer.drag"}
                    and action_payload.get("physical") is True
                ):
                    return None
                return result
            if self._seat_result_is_terminal_type_failure(action, result):
                return result
            return None
        except Exception:
            return None

    @staticmethod
    def _semantic_text_control_selector(payload: dict[str, Any]) -> dict[str, Any] | None:
        intent = str(payload.get("target_control") or payload.get("control_intent") or "").strip().lower()
        if intent not in {"browser_address", "browser_address_field"}:
            return None
        return {
            "roles": ["AXTextField", "AXComboBox", "AXTextArea"],
            "relative_region": {"min_x": 0.08, "max_x": 0.94, "min_y": 0.0, "max_y": 0.22},
            "require_enabled": True,
            "require_settable": True,
            "preference": "widest",
            "require_background": True,
            "forbidden_ancestor_roles": ["AXWebArea"],
        }

    def _try_semantic_background_text_control(
        self,
        action_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        selector = self._semantic_text_control_selector(action_payload)
        if selector is None:
            return None
        target = self._computer_seat_target(action_payload)
        target_error = str(target.get("_target_resolution_error") or "")
        if target_error:
            return {
                "action": "computer.type", "executed": False, "is_error": True,
                "background": True, "reason": target_error,
                "error_code": "TYPE_EXACT_WINDOW_NOT_FOUND",
            }
        bounds = target.get("window_bounds")
        if not target.get("pid") or not target.get("window_id") or not isinstance(bounds, dict) or len(bounds) != 4:
            return {
                "action": "computer.type", "executed": False, "is_error": True,
                "background": True,
                "reason": "Verified semantic text replacement requires an exact PID, window id, and window geometry.",
                "error_code": "TYPE_EXACT_WINDOW_REQUIRED",
            }
        try:
            result = self._get_computer_seat().set_text_control(
                target,
                text=str(action_payload.get("text") or ""),
                selector=selector,
            )
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("executed") and self._seat_result_is_background_safe(result):
            return self._background_action_success("computer.type", action_payload, result, platform.system())
        diagnostics = self._safe_semantic_text_diagnostics(result)
        return {
            "action": "computer.type", "executed": False, "is_error": True,
            "background": True,
            "driver": result.get("driver", "computer_seat") if isinstance(result, dict) else "computer_seat",
            "input_dispatched": diagnostics.get("input_dispatched") is True,
            "completion_verified": False,
            "diagnostics": diagnostics,
            "error_code": str(diagnostics.get("error_code") or "TYPE_SEMANTIC_BACKGROUND_FAILED"),
            "reason": "Verified background semantic text replacement failed; foreground replay was rejected.",
        }

    def _probe_semantic_text_control(self, action_payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve one semantic browser control without approval, text, or fallback."""
        selector = self._semantic_text_control_selector(action_payload)
        if selector is None:
            return self._semantic_probe_protocol_failure("TYPE_SEMANTIC_SELECTOR_INVALID")
        target = self._computer_seat_target(action_payload)
        target_error = str(target.get("_target_resolution_error") or "")
        if target_error:
            return self._semantic_probe_protocol_failure("TYPE_EXACT_WINDOW_NOT_FOUND")
        bounds = target.get("window_bounds")
        if not target.get("pid") or not target.get("window_id") or not isinstance(bounds, dict) or len(bounds) != 4:
            return self._semantic_probe_protocol_failure("TYPE_EXACT_WINDOW_REQUIRED")
        try:
            seat_result = self._get_computer_seat().probe_text_control(target, selector=selector)
        except Exception:
            seat_result = None
        diagnostics = self._safe_semantic_text_diagnostics(seat_result)
        protocol_complete = bool(
            isinstance(seat_result, dict)
            and seat_result.get("executed") is True
            and diagnostics.get("probe_completed") is True
            and isinstance(diagnostics.get("semantic_control_ready"), bool)
            and diagnostics.get("input_dispatched") is False
            and diagnostics.get("mutation_attempted") is False
            and diagnostics.get("semantic_discovery_stage")
        )
        if not protocol_complete:
            return self._semantic_probe_protocol_failure(
                str(diagnostics.get("error_code") or "TYPE_DIAGNOSTICS_INVALID")
            )
        ready = diagnostics.get("semantic_control_ready") is True
        return {
            "action": "computer.probe_text_control",
            "executed": True,
            "probe_completed": True,
            "semantic_control_ready": ready,
            "input_dispatched": False,
            "mutation_attempted": False,
            "background": True,
            "foreground": False,
            "requires_foreground": False,
            "uses_physical_input": False,
            "can_parallel_user_work": True,
            "diagnostics": diagnostics,
            **({"error_code": diagnostics["error_code"]} if diagnostics.get("error_code") else {}),
        }

    @staticmethod
    def _semantic_probe_protocol_failure(error_code: str) -> dict[str, Any]:
        if error_code == "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE":
            # Accept an old native helper response, but never emit its broad
            # subtree taxonomy from this current pack.
            error_code = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
        allowed_code = (
            error_code
            if error_code in {
                "TYPE_SEMANTIC_SELECTOR_INVALID", "TYPE_EXACT_WINDOW_REQUIRED",
                "TYPE_EXACT_WINDOW_NOT_FOUND", "TYPE_DIAGNOSTICS_INVALID",
                "TYPE_ACCESSIBILITY_NOT_TRUSTED", "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
                "TYPE_SEMANTIC_PROTOCOL_INVALID", "TYPE_BACKGROUND_PRECONDITION_FAILED",
                "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
                # Compatibility-only for older native helpers.
                "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
                "TYPE_SEMANTIC_PROBE_UNAVAILABLE", "TYPE_SEMANTIC_PROBE_FAILED",
                "TYPE_SEMANTIC_PROBE_UNSAFE_RESULT",
            }
            else "TYPE_DIAGNOSTICS_INVALID"
        )
        return {
            "action": "computer.probe_text_control",
            "executed": False,
            "probe_completed": False,
            "semantic_control_ready": False,
            "input_dispatched": False,
            "mutation_attempted": False,
            "background": True,
            "foreground": False,
            "requires_foreground": False,
            "uses_physical_input": False,
            "is_error": True,
            "error_code": allowed_code,
            "reason": "Semantic text-control probe protocol failed.",
            "diagnostics": {
                "probe_completed": False,
                "semantic_control_ready": False,
                "input_dispatched": False,
                "mutation_attempted": False,
                "error_code": allowed_code,
            },
        }

    @staticmethod
    def _safe_semantic_text_diagnostics(result: Any) -> dict[str, Any]:
        raw = BrowserComputerController._type_diagnostics(result)
        safe: dict[str, Any] = {}
        for key in (
            "completion_verified", "input_dispatched", "target_window_stable",
            "semantic_control_resolved", "semantic_control_role_allowed",
            "semantic_control_settable", "focus_attempted", "focus_succeeded",
            "focused_control_matches", "selection_verified",
            "value_readback_attempted", "value_readback_matched", "mutation_observed",
            "semantic_counts_truncated", "saw_ax_text_field", "saw_ax_combo_box",
            "saw_ax_text_area", "saw_ax_search_field_subrole",
            "saw_ax_web_area_ancestor", "saw_unlisted_text_capable_role",
            "window_frame_match", "child_frame_valid", "child_center_inside_window",
            "relative_region_evaluable", "relative_region_matched",
            "probe_completed", "semantic_control_ready", "mutation_attempted",
            "semantic_window_scan_complete", "semantic_window_scan_truncated",
            "semantic_window_depth_truncated", "semantic_app_scan_performed",
            "semantic_app_scan_complete", "semantic_app_scan_truncated",
            "saw_unlisted_container_class", "saw_unlisted_static_value_class",
            "saw_unlisted_action_control_class", "saw_unlisted_web_root_class",
            "saw_unlisted_other_class",
            "semantic_children_failure_on_window_root", "semantic_children_failure_under_toolbar",
            "semantic_children_attribute_advertised", "semantic_children_count_known",
            "semantic_children_count_nonzero", "semantic_children_branch_proven_empty",
            "semantic_actionable_branch_scope_complete", "semantic_actionable_candidates_complete",
            "semantic_actionable_scan_complete", "semantic_stale_node_self_eligible",
            "semantic_stale_recovery_eligible", "semantic_stale_recovery_attempted",
            "semantic_stale_recovery_window_rebound", "semantic_stale_recovery_window_stable",
            "semantic_stale_recovery_second_pass_complete", "semantic_stale_recovery_succeeded",
            "semantic_stale_parent_refresh_attempted", "semantic_stale_parent_refresh_succeeded",
            "semantic_stale_recovery_final_scan_complete",
            "semantic_stale_additional_read_budget_exhausted",
            "exact_binding_input_valid", "exact_running_app_present",
            "exact_quartz_query_completed", "exact_quartz_record_present",
            "exact_quartz_owner_matches", "exact_quartz_layer_allowed", "exact_quartz_visible",
            "exact_quartz_frame_matches", "exact_ax_windows_attribute_available",
            "exact_ax_windows_payload_valid", "exact_ax_windows_read_completed",
            "exact_ax_match_present", "exact_ax_match_unique", "exact_window_resolved",
            "exact_resolution_retry_attempted", "exact_resolution_retry_recovered",
            "native_frontmost_check_completed", "native_target_non_frontmost_before",
            "native_target_non_frontmost_after", "native_frontmost_unchanged",
            "semantic_actionable_counts_truncated", "semantic_app_diagnostic_counts_truncated",
            "semantic_unlisted_relation_scan_complete",
            "semantic_exposure_probe_performed", "semantic_exposure_probe_complete",
            "semantic_exposure_probe_truncated", "semantic_alt_contents_advertised",
            "semantic_exposure_global_node_limit_hit", "semantic_exposure_global_read_limit_hit",
            "semantic_exposure_count_saturated",
            "semantic_alt_visible_children_advertised", "semantic_alt_navigation_order_advertised",
            "semantic_alt_shared_text_advertised", "semantic_alt_focused_element_present",
            "semantic_alt_focused_element_exact_owned", "semantic_alt_focused_element_non_web",
            "semantic_alt_focused_element_allowed_role", "semantic_alt_search_predicate_advertised",
            "semantic_alt_text_marker_relation_advertised", "semantic_alt_allowed_role_found",
            "semantic_alt_full_eligibility_found",
            "semantic_navigation_order_count_stable", "semantic_navigation_order_complete",
        ):
            if isinstance(raw.get(key), bool):
                safe[key] = raw[key]
        count_caps = {
            "semantic_nodes_visited_count": 255,
            "semantic_role_match_count": 64,
            "semantic_window_owned_count": 64,
            "semantic_non_web_content_count": 64,
            "semantic_frame_valid_count": 64,
            "semantic_region_match_count": 64,
            "semantic_enabled_count": 64,
            "semantic_value_present_count": 64,
            "semantic_value_readable_count": 64,
            "semantic_value_settable_count": 64,
            "semantic_selected_text_settable_count": 64,
            "semantic_selected_range_settable_count": 64,
            "semantic_focus_settable_count": 64,
            "semantic_final_candidate_count": 8,
            "semantic_preinvalidation_candidate_count": 8,
            "semantic_window_nodes_visited_count": 255,
            "semantic_window_duplicate_nodes_skipped_count": 255,
            "semantic_window_max_depth_reached": 20,
            "semantic_app_nodes_visited_count": 255,
            "semantic_forbidden_root_count": 64,
            "semantic_forbidden_subtree_pruned_count": 64,
            "semantic_other_window_pruned_count": 64,
            "semantic_children_read_failure_count": 64,
            "semantic_children_read_success_count": 64,
            "semantic_children_empty_count": 64,
            "semantic_children_unsupported_count": 64,
            "semantic_children_no_value_count": 64,
            "semantic_children_cannot_complete_count": 64,
            "semantic_children_invalid_element_count": 64,
            "semantic_children_global_failure_count": 64,
            "semantic_children_protocol_failure_count": 64,
            "semantic_children_unknown_branch_count": 64,
            "semantic_unresolved_selector_branch_count": 64,
            "semantic_children_proven_empty_after_failure_count": 64,
            "semantic_children_retry_attempted_count": 64,
            "semantic_children_retry_recovered_count": 64,
            "semantic_stale_parent_refresh_count": 1,
            "semantic_stale_parent_refresh_read_count": 2,
            "semantic_stale_additional_ax_read_count": 64,
            "semantic_discovery_pass_count": 3,
            "semantic_stale_recovery_restart_count": 2,
            "semantic_first_pass_stale_count": 64,
            "semantic_second_pass_stale_count": 64,
            "semantic_first_pass_unknown_branch_count": 64,
            "semantic_second_pass_unknown_branch_count": 64,
            "semantic_first_pass_nodes_visited_count": 255,
            "semantic_second_pass_nodes_visited_count": 255,
            "semantic_second_pass_final_candidate_count": 8,
            "semantic_third_pass_stale_count": 64,
            "semantic_third_pass_unknown_branch_count": 64,
            "semantic_third_pass_nodes_visited_count": 255,
            "semantic_third_pass_final_candidate_count": 8,
            "semantic_navigation_order_fallback_attempted_count": 8,
            "semantic_navigation_order_fallback_succeeded_count": 8,
            "semantic_navigation_order_recovered_invalid_count": 8,
            "semantic_navigation_order_page_read_count": 16,
            "semantic_window_allowed_role_count": 64,
            "semantic_app_owned_allowed_role_count": 64,
            "semantic_allowed_ax_text_field_count": 8,
            "semantic_allowed_ax_combo_box_count": 8,
            "semantic_allowed_ax_text_area_count": 8,
            "semantic_allowed_frame_inside_window_count": 8,
            "semantic_allowed_region_x_match_count": 8,
            "semantic_allowed_region_y_match_count": 8,
            "semantic_unlisted_text_capable_count": 64,
            "semantic_unlisted_window_owned_count": 64,
            "semantic_unlisted_non_web_count": 64,
            "semantic_unlisted_frame_valid_count": 64,
            "semantic_unlisted_region_match_count": 64,
            "semantic_unlisted_enabled_count": 64,
            "semantic_unlisted_value_readable_count": 64,
            "semantic_unlisted_mutation_ready_count": 64,
            "semantic_unlisted_value_settable_count": 64,
            "semantic_unlisted_selected_text_settable_count": 64,
            "semantic_unlisted_selected_range_settable_count": 64,
            "semantic_unlisted_focus_settable_count": 64,
            "semantic_unlisted_attribute_capability_known_count": 64,
            "semantic_unlisted_under_toolbar_count": 64,
            "semantic_unlisted_related_allowed_role_count": 64,
            "exact_resolution_attempt_count": 2, "exact_quartz_record_match_count": 2,
            "exact_ax_window_count": 16, "exact_ax_frame_valid_count": 16,
            "exact_ax_frame_match_count": 8,
            "semantic_exposure_nodes_visited_count": 64,
            "semantic_exposure_edge_reads_count": 128,
            "semantic_exposure_edge_read_failure_count": 16,
            "semantic_exposure_exact_owned_count": 64,
            "semantic_exposure_non_web_count": 64,
            "semantic_exposure_allowed_role_count": 8,
            "semantic_exposure_full_eligibility_count": 8,
            "semantic_exposure_shared_text_relation_count": 8,
            "semantic_exposure_parameterized_capability_count": 8,
            "semantic_exposure_page_control_count": 8,
            "semantic_exposure_incomplete_cause_count": 8,
            "semantic_exposure_edge_fanout_truncated_count": 16,
            "semantic_exposure_depth_limit_new_target_count": 16,
            "semantic_exposure_depth_limit_queued_target_count": 16,
            "semantic_exposure_queue_remainder_count": 64,
            "semantic_exposure_payload_missing_count": 16,
            "semantic_exposure_payload_invalid_count": 16,
            "semantic_exposure_payload_mixed_count": 16,
            "semantic_exposure_attribute_inventory_unknown_count": 16,
            "semantic_exposure_parameterized_inventory_unknown_count": 5,
            "semantic_exposure_edge_incomplete_without_failure_count": 16,
            "semantic_exposure_node_ownership_rejected_count": 64,
            "semantic_exposure_edge_target_ownership_rejected_count": 64,
        }
        counts_truncated = safe.get("semantic_counts_truncated") is True
        for key, cap in count_caps.items():
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                safe[key] = max(0, min(cap, value))
                counts_truncated = counts_truncated or value < 0 or value > cap
        if any(key in safe for key in count_caps):
            safe["semantic_counts_truncated"] = counts_truncated
        error_code = str(raw.get("error_code") or "")
        if error_code == "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE":
            error_code = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
        if error_code in {
            "TEXT_REQUIRED", "TYPE_ACCESSIBILITY_NOT_TRUSTED", "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
            "TYPE_SEMANTIC_PROTOCOL_INVALID", "TYPE_EXACT_WINDOW_REQUIRED",
            "TYPE_EXACT_WINDOW_NOT_FOUND", "TYPE_BACKGROUND_PRECONDITION_FAILED",
            "TYPE_SEMANTIC_SELECTOR_INVALID", "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
            "TYPE_SEMANTIC_CONTROL_DISABLED", "TYPE_SEMANTIC_VALUE_UNREADABLE",
            "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE", "TYPE_SEMANTIC_CONTROL_AMBIGUOUS",
            "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED", "TYPE_SEMANTIC_COORDINATE_MISMATCH",
            "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
            "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
            # Compatibility-only for older native helpers.
            "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
            "TYPE_SEMANTIC_ROLE_CLASS_UNRESOLVED",
            "TYPE_SEMANTIC_PROBE_UNAVAILABLE", "TYPE_SEMANTIC_PROBE_FAILED",
            "TYPE_SEMANTIC_PROBE_UNSAFE_RESULT",
            "TYPE_EXACT_WINDOW_INPUT_INVALID", "TYPE_EXACT_WINDOW_APP_NOT_RUNNING",
            "TYPE_EXACT_WINDOW_QUARTZ_RECORD_NOT_FOUND", "TYPE_EXACT_WINDOW_QUARTZ_RECORD_INVALID",
            "TYPE_EXACT_WINDOW_FRAME_MISMATCH", "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE",
            "TYPE_EXACT_WINDOW_AX_MATCH_NOT_FOUND", "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS",
            "TYPE_SELECTION_INVALID",
            "TYPE_COMPLETION_NOT_VERIFIED", "TYPE_TARGET_DRIFTED",
        }:
            safe["error_code"] = error_code
        strategy = str(raw.get("input_strategy") or "")
        if strategy in {"none", "semantic_ax_selected_text", "semantic_ax_value"}:
            safe["input_strategy"] = strategy
        failure_stage = str(raw.get("failure_stage") or "")
        if failure_stage in {
            "accessibility_permission", "exact_window_binding", "exact_window_resolution",
            "selector_validation", "background_precondition", "semantic_control_resolution",
            "semantic_control_validation", "selection_verification", "same_element_readback",
        }:
            safe["failure_stage"] = failure_stage
        for key, allowed in {
            "semantic_scan_scope": {"exact_window_descendants", "application_tree_owned", "none"},
            "semantic_discovery_stage": {
                "no_nodes", "scan_incomplete", "role_absent", "window_ownership_unverified", "web_content_excluded",
                "frame_unavailable", "region_excluded", "disabled", "value_unreadable",
                "not_settable", "ambiguous", "ready",
            },
            "semantic_coordinate_status": {
                "window_frame_matched", "child_frames_unavailable", "child_frames_outside_window",
                "relative_region_miss", "consistent", "unavailable",
            },
            "semantic_ownership_proof": {
                "window_descendant", "ax_window_attribute", "top_level_ui_element",
                "ancestor_chain", "none",
            },
            "semantic_traversal_order": {"breadth_first"},
            "semantic_unlisted_role_class": {
                "unlisted_container", "unlisted_static_value", "unlisted_action_control",
                "unlisted_web_root", "unlisted_other", "multiple", "none",
            },
            "semantic_app_diagnostic_stage": {"not_performed", "complete", "scan_incomplete"},
            "semantic_app_diagnostic_scope": {"application_tree_owned", "none"},
            "semantic_app_diagnostic_ownership_proof": {
                "ax_window_attribute", "top_level_ui_element", "ancestor_chain", "multiple", "none",
            },
            "semantic_unlisted_relation_kind": {
                "title_relation", "linked_relation", "parent_child", "none", "multiple",
            },
            "semantic_allowed_role_class": {
                "ax_text_field", "ax_combo_box", "ax_text_area", "multiple", "none",
            },
            "semantic_allowed_region_miss_axis": {
                "none", "x", "y", "both", "outside_window", "frame_unavailable", "multiple",
            },
            "semantic_allowed_center_y_band": {
                "top_0_22", "upper_22_35", "middle_35_65", "lower_65_100",
                "outside_window", "frame_unavailable", "multiple", "none",
            },
            "semantic_allowed_width_band": {
                "narrow_lt_40", "wide_40_80", "near_full_80_100",
                "outside_window", "frame_unavailable", "multiple", "none",
            },
            "semantic_allowed_height_band": {
                "shallow_0_15", "medium_15_40", "tall_40_100",
                "outside_window", "frame_unavailable", "multiple", "none",
            },
            "semantic_children_failure_class": {
                "none", "cannot_complete", "stale_element", "global_api", "protocol",
                "generic", "multiple",
            },
            "semantic_children_incomplete_branch_class": {
                "window_root", "container", "static_value", "action_control", "other",
                "multiple", "none",
            },
            "semantic_children_ax_error_class": {
                "none", "no_value", "attribute_unsupported", "cannot_complete",
                "invalid_element", "api_disabled", "not_implemented", "illegal_argument",
                "payload_type_invalid", "generic", "multiple",
            },
            "semantic_children_structural_empty_proof": {
                "none", "count_zero", "attribute_not_advertised", "multiple",
            },
            "semantic_navigation_order_fallback_outcome": {
                "not_attempted", "complete_empty", "complete_children", "unavailable",
                "incomplete", "protocol_invalid", "multiple",
            },
            "semantic_navigation_order_failure_class": {
                "none", "not_advertised", "count_unavailable", "count_over_limit",
                "page_ax_failure", "payload_invalid", "count_changed", "duplicate",
                "self_cycle", "parent_unavailable", "parent_mismatch", "multiple",
            },
            "semantic_navigation_order_ax_error_class": {
                "none", "no_value", "attribute_unsupported", "cannot_complete",
                "invalid_element", "api_disabled", "not_implemented", "illegal_argument",
                "generic", "multiple",
            },
            "semantic_navigation_order_cardinality_class": {
                "zero", "one", "two_to_eight", "nine_to_64", "sixty_five_to_255",
                "over_limit", "unknown", "multiple",
            },
            "semantic_navigation_order_parent_proof": {
                "not_checked", "empty", "all_direct", "unavailable", "mismatch", "multiple",
            },
            "semantic_stale_branch_scope": {
                "none", "structurally_empty", "forbidden_web", "candidate_node",
                "selector_relevant_unknown", "window_root", "multiple", "unknown",
            },
            "accessibility_trust_preflight": {"granted", "denied"},
            "semantic_stale_node_class": {
                "none", "container", "text_control", "static_value", "action_control",
                "other", "multiple",
            },
            "semantic_stale_recovery_outcome": {
                "not_needed", "recovered_clean", "recovery_not_eligible",
                "exact_window_rebind_failed", "exact_window_changed", "frontmost_changed",
                # Older helpers can remain observable during the rollout.
                "second_pass_stale", "second_pass_incomplete",
                "parent_refresh_not_eligible", "parent_refresh_failed",
                "parent_refresh_budget_exhausted", "recovered_after_parent_refresh",
                "final_pass_stale", "final_pass_incomplete",
            },
            "semantic_stale_reference_refresh_class": {
                "not_attempted", "same_stale_reference_returned",
                "stale_reference_absent_nonempty", "branch_now_empty", "unknown",
            },
            "semantic_stale_branch_comparison": {
                "not_applicable", "same_class_and_depth", "different_class_or_depth",
                "multiple", "unknown",
            },
            "semantic_second_third_stale_reference_class": {
                "same_parent_same_reference", "same_parent_new_reference",
                "new_parent_same_reference", "new_parent_new_reference", "not_comparable",
            },
            "exact_resolution_stage": {
                "input_validation", "running_application", "quartz_record", "quartz_frame",
                "ax_window_enumeration", "ax_window_match", "background_validation", "ready",
            },
            "exact_resolution_outcome": {
                "input_invalid", "application_not_running", "quartz_record_missing",
                "quartz_record_invalid", "quartz_frame_mismatch", "ax_windows_unavailable",
                "ax_match_absent", "ax_match_ambiguous", "frontmost_changed", "ready", "recovered",
            },
            "ax_windows_outcome": {
                "success", "no_value", "unsupported", "cannot_complete",
                "invalid_application_element", "global_failure", "protocol_invalid",
            },
            "semantic_exposure_stage": {
                "incomplete", "alternate_structural_role_found", "relationship_role_found",
                "focused_page_control", "capability_advertised_only", "only_unlisted_proxy",
                "complete_no_fixed_exposure",
            },
            "semantic_exposure_source": {
                "contents", "visible_children", "navigation_order", "shared_text",
                "focused_element", "multiple", "none",
            },
            "semantic_parameterized_capability_class": {
                "search_predicate", "text_marker_relation", "multiple", "none",
            },
            "semantic_exposure_incomplete_cause": {
                "none", "edge_fanout", "depth_limit", "global_node_limit",
                "global_read_limit", "queue_remainder", "focus_cardinality",
                "payload_invalid", "attribute_inventory_unknown",
                "parameterized_inventory_unknown", "edge_incomplete_without_failure",
                "counter_saturation", "multiple",
            },
            "semantic_exposure_fanout_source": {
                "contents", "visible_children", "navigation_order", "shared_text",
                "title_relation", "serves_as_title", "linked", "parent", "multiple", "none",
            },
            "semantic_exposure_depth_limit_source": {
                "contents", "visible_children", "navigation_order", "shared_text",
                "title_relation", "serves_as_title", "linked", "parent", "multiple", "none",
            },
            "semantic_exposure_focus_cardinality": {"none", "one", "multiple", "unknown"},
            "semantic_exposure_count_saturation_class": {
                "none", "incomplete_cause_count", "edge_fanout",
                "depth_limit_new_target", "depth_limit_queued_target", "queue_remainder",
                "payload_missing", "payload_invalid", "payload_mixed",
                "attribute_inventory_unknown", "parameterized_inventory_unknown",
                "edge_incomplete_without_failure", "node_ownership_rejected",
                "edge_target_ownership_rejected", "nodes_visited", "edge_reads",
                "edge_read_failures", "exact_owned", "non_web", "allowed_role",
                "full_eligibility", "shared_text_relation", "parameterized_capability",
                "page_control", "multiple",
            },
        }.items():
            value = str(raw.get(key) or "")
            if value in allowed:
                safe[key] = value
        return safe

    def _desktop_action(self, action: str, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        dry_run = self._truthy(payload.get("dry_run"))
        if dry_run:
            return {"action": action, "dry_run": True, "requires_approval": False, "payload": payload}
        if action == "computer.type" and not str(payload.get("text") or ""):
            return {
                "action": action,
                "executed": False,
                "is_error": True,
                "platform": platform.system(),
                "reason": "computer.type requires non-empty text. Use computer.key for shortcuts or individual key presses.",
                "recovery": {
                    "kind": "invalid_type_payload",
                    "note": "For shortcuts, retry with action=key and key/modifier/modifiers/key_combo.",
                },
            }
        system = platform.system()
        action_payload = dict(payload)
        click_marker = None
        drag_marker = None
        if action == "computer.drag":
            action_payload, click_marker, drag_marker = self._resolve_drag_points(payload, remember_cursor=False)
        elif action in {"computer.move", "computer.click"}:
            action_payload, click_marker = self._resolve_action_point(payload, infer_window=action == "computer.click", remember_cursor=False)
        virtual_only = self._pointer_action_is_virtual_only(action, payload)
        approval_payload = self._desktop_approval_payload(action, payload, action_payload, virtual_only=virtual_only)
        if not (yolo_mode or self._consume_approval(payload, action, approval_payload)):
            return self._approval_required(action, approval_payload)
        foreground_fallback_requested = self._foreground_fallback_requested(action_payload)
        background_only = self._background_requested(action_payload) and not foreground_fallback_requested
        remember_pointer = not background_only
        if action == "computer.drag":
            action_payload, click_marker, drag_marker = self._resolve_drag_points(payload, remember_cursor=remember_pointer)
        elif action in {"computer.move", "computer.click"}:
            action_payload, click_marker = self._resolve_action_point(
                payload,
                infer_window=action == "computer.click",
                remember_cursor=remember_pointer,
            )
        virtual_only = self._pointer_action_is_virtual_only(action, payload)
        if action == "computer.type" and self._semantic_text_control_selector(action_payload) is not None:
            with self._edge_haze(action, action_payload) as edge_haze:
                semantic_result = self._try_semantic_background_text_control(action_payload)
            assert semantic_result is not None
            self._attach_edge_haze_result(semantic_result, edge_haze)
            return semantic_result
        if background_only:
            with self._edge_haze(action, action_payload) as edge_haze:
                seat_result = self._try_computer_seat_action(action, action_payload, background_safe_only=True)
            if seat_result is not None and seat_result.get("executed"):
                if self._seat_result_is_background_safe(seat_result):
                    result = self._background_action_success(action, action_payload, seat_result, system)
                    self._attach_edge_haze_result(result, edge_haze)
                    if action in {"computer.move", "computer.click"}:
                        resolved = {"x": int(action_payload.get("x", 0)), "y": int(action_payload.get("y", 0))}
                        if "target" in result:
                            result["resolved_coordinates"] = resolved
                        else:
                            result["target"] = resolved
                        if click_marker:
                            result["marker"] = click_marker
                    if action == "computer.drag":
                        result["target"] = {
                            "from": {"x": int(action_payload.get("x1", 0)), "y": int(action_payload.get("y1", 0))},
                            "to": {"x": int(action_payload.get("x2", 0)), "y": int(action_payload.get("y2", 0))},
                        }
                        if drag_marker:
                            result["drag_marker"] = drag_marker
                    return result
            return self._background_visible_window_required(action, action_payload, system, seat_result=seat_result)
        if action in {"computer.move", "computer.click", "computer.drag"} and virtual_only:
            pointer = self._set_ai_cursor(action_payload)
            pointer_overlay = self._publish_virtual_pointer(pointer, action=action, payload=action_payload)
            result: dict[str, Any] = {"action": action, "executed": True, "platform": system, "virtual_cursor": True}
            result["ai_cursor"] = pointer
            if pointer_overlay:
                result["virtual_pointer_overlay"] = pointer_overlay
            if action == "computer.drag":
                result["target"] = {
                    "from": {"x": int(action_payload.get("x1", 0)), "y": int(action_payload.get("y1", 0))},
                    "to": {"x": int(action_payload.get("x2", 0)), "y": int(action_payload.get("y2", 0))},
                }
                if drag_marker:
                    result["drag_marker"] = drag_marker
            else:
                result["target"] = {"x": int(action_payload.get("x", 0)), "y": int(action_payload.get("y", 0))}
            if click_marker:
                result["marker"] = click_marker
            if self._should_capture_after_action(action, payload):
                screenshot = self._capture_action_result_screenshot(
                    action_payload,
                    click_marker,
                    action_name=action,
                    drag_marker=drag_marker,
                )
                result.update(screenshot)
            return result
        if self._should_try_background_seat_before_focus(action, action_payload):
            with self._edge_haze(action, action_payload) as edge_haze:
                seat_result = self._try_computer_seat_action(
                    action,
                    action_payload,
                    background_safe_only=True,
                    verified_background_only=True,
                )
            if seat_result is not None and seat_result.get("executed") and self._seat_result_is_background_safe(seat_result):
                if self._seat_result_is_verified_background(seat_result):
                    result = self._background_action_success(action, action_payload, seat_result, system)
                    self._attach_edge_haze_result(result, edge_haze)
                    return result
                executed_count = int(seat_result.get("executed_count") or 0)
                requested_count = int(seat_result.get("requested_count") or executed_count)
                if executed_count > 0 and executed_count < requested_count:
                    result = self._background_action_success(action, action_payload, seat_result, system)
                    self._attach_edge_haze_result(result, edge_haze)
                    result["partial_success"] = True
                    result["is_error"] = True
                    result["reason"] = "Background key sequence partially executed; refusing foreground replay."
                    return result
            if seat_result is not None:
                return self._background_visible_window_required(
                    action,
                    action_payload,
                    system,
                    implicit=True,
                    seat_result=seat_result,
                )
        if action in {"computer.type", "computer.key", "computer.scroll"} and action_payload.get("focus") is False:
            return {
                "action": action,
                "executed": False,
                "is_error": True,
                "platform": system,
                "reason": "Foreground input requires focus because background input is disabled.",
                "recovery": {
                    "kind": "focus_required",
                    "note": "Use a visible selected window with focus=true or omit focus=false.",
                },
            }
        if action in {"computer.type", "computer.key", "computer.scroll"}:
            self._focus_action_target(action_payload)
        if action == "computer.drag" and payload.get("physical") is True:
            self._focus_action_target(action_payload)
        foreground_error = self._foreground_action_focus_error(action, action_payload)
        if foreground_error is not None:
            return foreground_error
        # --- Attempt ComputerSeatService delegation ---
        with self._edge_haze(action, action_payload) as edge_haze:
            seat_result = self._try_computer_seat_action(action, action_payload)
        if seat_result is not None and seat_result.get("executed"):
            result: dict[str, Any] = {"action": action, "executed": True, "platform": system}
            result["driver"] = seat_result.get("driver", "computer_seat")
            result["is_fallback"] = seat_result.get("is_fallback", False)
            self._attach_edge_haze_result(result, edge_haze)
            if action == "computer.type" and isinstance(seat_result.get("data"), dict):
                seat_data = seat_result["data"]
                if "completion_verified" in seat_data:
                    result["completion_verified"] = seat_data.get("completion_verified") is True
                if seat_data.get("completion_check"):
                    result["completion_check"] = str(seat_data["completion_check"])
            if action in {"computer.move", "computer.click"}:
                result["target"] = {"x": int(action_payload.get("x", 0)), "y": int(action_payload.get("y", 0))}
                if click_marker:
                    result["marker"] = click_marker
            if action == "computer.drag":
                result["target"] = {
                    "from": {"x": int(action_payload.get("x1", 0)), "y": int(action_payload.get("y1", 0))},
                    "to": {"x": int(action_payload.get("x2", 0)), "y": int(action_payload.get("y2", 0))},
                }
                if click_marker:
                    result["marker"] = click_marker
                if drag_marker:
                    result["drag_marker"] = drag_marker
            if action == "computer.scroll":
                result["amount"] = int(action_payload.get("amount", 1))
            if self._should_capture_after_action(action, payload):
                screenshot = self._capture_action_result_screenshot(
                    action_payload,
                    click_marker,
                    action_name=action,
                    drag_marker=drag_marker,
                )
                result.update(screenshot)
            if action == "computer.type" and self._seat_type_completion_verified(seat_result):
                self._with_computer_type_success_guidance(result)
            elif action == "computer.type":
                self._mark_type_delivery_unverified(result, seat_result)
            return result
        if self._seat_result_is_terminal_type_failure(action, seat_result):
            notes = seat_result.get("notes") if isinstance(seat_result, dict) else None
            reason = (
                str(notes[0])
                if isinstance(notes, list) and notes
                else "Native text input was dispatched, but full completion was not verified."
            )
            result = {
                "action": action,
                "executed": False,
                "is_error": True,
                "platform": system,
                "driver": seat_result.get("driver", "computer_seat"),
                "input_dispatched": self._type_diagnostics(seat_result).get("input_dispatched") is True,
                "completion_verified": False,
                "diagnostics": self._type_diagnostics(seat_result),
                "reason": reason,
                "recovery": {
                    "kind": "type_completion_unverified",
                    "note": "Inspect the focused field before deciding whether another text action is safe.",
                },
            }
            self._attach_edge_haze_result(result, edge_haze)
            return result
        # --- Legacy platform-specific fallback ---
        with self._edge_haze(action, action_payload) as edge_haze:
            if system == "Darwin" and action == "computer.move":
                self._darwin_move_cursor(action_payload)
            elif system == "Darwin" and action == "computer.click":
                self._darwin_click(action_payload)
            elif system == "Darwin" and action == "computer.drag":
                self._darwin_drag(action_payload)
            elif system == "Darwin" and action == "computer.type":
                self._darwin_type(action_payload)
            elif system == "Darwin":
                script = self._apple_script(action, action_payload)
                subprocess.run(["osascript", "-e", script], check=True, timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS)
            elif system == "Windows":
                self._windows_desktop_action(action, action_payload)
            else:
                return {
                    "action": action,
                    "supported": False,
                    "platform": system,
                    "reason": "Desktop actions are supported on macOS, Windows, and Linux when a visible desktop driver is available.",
                }
        result: dict[str, Any] = {"action": action, "executed": True, "platform": system}
        self._attach_edge_haze_result(result, edge_haze)
        if action in {"computer.type", "computer.key", "computer.scroll"}:
            result["driver"] = "foreground_input"
        if action == "computer.key":
            result["count"] = _key_press_count(action_payload)
        if action in {"computer.move", "computer.click"}:
            result["target"] = {"x": int(action_payload.get("x", 0)), "y": int(action_payload.get("y", 0))}
            if click_marker:
                result["marker"] = click_marker
        if action == "computer.drag":
            result["target"] = {
                "from": {"x": int(action_payload.get("x1", 0)), "y": int(action_payload.get("y1", 0))},
                "to": {"x": int(action_payload.get("x2", 0)), "y": int(action_payload.get("y2", 0))},
            }
            if click_marker:
                result["marker"] = click_marker
            if drag_marker:
                result["drag_marker"] = drag_marker
        if action == "computer.scroll":
            result["amount"] = int(action_payload.get("amount", 1))
        if self._should_capture_after_action(action, payload):
            screenshot = self._capture_action_result_screenshot(
                action_payload,
                click_marker,
                action_name=action,
                drag_marker=drag_marker,
            )
            result.update(screenshot)
        if action == "computer.type":
            self._with_computer_type_success_guidance(result)
        return result

    @staticmethod
    def _seat_result_has_dispatched_type_input(action: str, result: Any) -> bool:
        if action != "computer.type" or not isinstance(result, dict):
            return False
        data = result.get("data")
        return isinstance(data, dict) and data.get("input_dispatched") is True

    @staticmethod
    def _type_diagnostics(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        data = result.get("data")
        if not isinstance(data, dict):
            return {}
        diagnostics = data.get("diagnostics")
        return dict(diagnostics) if isinstance(diagnostics, dict) else dict(data)

    @classmethod
    def _seat_result_is_terminal_type_failure(cls, action: str, result: Any) -> bool:
        if action != "computer.type" or not isinstance(result, dict) or result.get("executed"):
            return False
        diagnostics = cls._type_diagnostics(result)
        return (
            diagnostics.get("input_dispatched") is True
            or diagnostics.get("direct_ax_attempted") is True
            or bool(diagnostics.get("failure_stage"))
            or bool(diagnostics.get("error_code"))
        )

    @staticmethod
    def _should_try_background_seat_before_focus(action: str, payload: dict[str, Any]) -> bool:
        if action not in {"computer.type", "computer.key", "computer.scroll"}:
            return False
        if payload.get("physical") is True:
            return False
        if BrowserComputerController._foreground_fallback_requested(payload):
            return False
        return True

    @staticmethod
    def _background_action_success(
        action: str,
        action_payload: dict[str, Any],
        seat_result: dict[str, Any],
        system: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": action,
            "executed": True,
            "platform": system,
            "background": True,
            "driver": seat_result.get("driver", "computer_seat"),
            "confidence": seat_result.get("confidence", "best_effort"),
            "can_parallel_user_work": seat_result.get("can_parallel_user_work"),
            "requires_foreground": seat_result.get("requires_foreground"),
            "uses_physical_input": seat_result.get("uses_physical_input"),
        }
        if seat_result.get("notes"):
            result["notes"] = seat_result.get("notes")
        if seat_result.get("data") and action != "computer.type":
            result["target"] = seat_result.get("data")
        ax_candidate = BrowserComputerController._safe_ax_candidate_diagnostics(seat_result)
        if ax_candidate:
            result["ax_candidate"] = ax_candidate
        if action == "computer.key":
            requested_count = int(seat_result.get("requested_count") or _key_press_count(action_payload))
            executed_count = int(seat_result.get("executed_count") or seat_result.get("count") or requested_count)
            result["count"] = executed_count
            result["requested_count"] = requested_count
            result["executed_count"] = executed_count
            if executed_count < requested_count:
                result["partial_success"] = True
        if action == "computer.scroll":
            result["amount"] = int(action_payload.get("amount", action_payload.get("clicks", 1)))
        if action == "computer.type" and BrowserComputerController._seat_type_completion_verified(seat_result):
            result["completion_verified"] = True
            BrowserComputerController._with_computer_type_success_guidance(result)
        elif action == "computer.type":
            BrowserComputerController._mark_type_delivery_unverified(result, seat_result)
        return result

    @staticmethod
    def _seat_result_is_background_safe(seat_result: dict[str, Any]) -> bool:
        if seat_result.get("executed") is not True:
            return False
        if seat_result.get("uses_physical_input") is not False:
            return False
        if seat_result.get("requires_foreground") is not False:
            return False
        if seat_result.get("can_parallel_user_work") is not True:
            return False
        driver = str(seat_result.get("driver") or "").strip()
        if driver in {
            "browser_cdp",
            "linux_x11_virtual",
            "windows_postmessage",
            "windows_uia",
        }:
            return True
        if driver in {"mac_accessibility", "mac_cgevent_pid"}:
            return True
        if driver == "mac_swift_host" and str(seat_result.get("action") or "") == "set_text_control":
            return BrowserComputerController._seat_type_completion_verified(seat_result)
        return False

    @staticmethod
    def _seat_result_is_verified_background(seat_result: dict[str, Any]) -> bool:
        if not BrowserComputerController._seat_result_is_background_safe(seat_result):
            return False
        confidence = str(seat_result.get("confidence") or "").strip().lower()
        if confidence in {"experimental", "best_effort", "posted_only", "posted only"}:
            return False
        driver = str(seat_result.get("driver") or "").strip()
        if str(seat_result.get("action") or "") == "type_text":
            return BrowserComputerController._seat_type_completion_verified(seat_result)
        if str(seat_result.get("action") or "") == "set_text_control":
            return BrowserComputerController._seat_type_completion_verified(seat_result)
        if str(seat_result.get("action") or "") == "key":
            return BrowserComputerController._seat_key_effect_verified(seat_result)
        return driver in {"browser_cdp", "browser_companion", "mac_accessibility", "windows_uia"}

    @staticmethod
    def _seat_type_completion_verified(seat_result: Any) -> bool:
        if not isinstance(seat_result, dict):
            return False
        if seat_result.get("completion_verified") is True:
            return True
        data = seat_result.get("data")
        if not isinstance(data, dict):
            return False
        if data.get("completion_verified") is True:
            return True
        diagnostics = data.get("diagnostics")
        return isinstance(diagnostics, dict) and diagnostics.get("completion_verified") is True

    @staticmethod
    def _seat_key_effect_verified(seat_result: Any) -> bool:
        if not isinstance(seat_result, dict):
            return False
        if seat_result.get("completion_verified") is True or seat_result.get("postcondition_verified") is True:
            return True
        data = seat_result.get("data")
        if not isinstance(data, dict):
            return False
        return data.get("completion_verified") is True or data.get("postcondition_verified") is True

    @staticmethod
    def _mark_type_delivery_unverified(result: dict[str, Any], seat_result: Any) -> None:
        result.update(
            {
                "executed": True,
                "delivered": True,
                "input_dispatched": True,
                "completion_verified": False,
                "effect_observed": False,
                "postcondition_verified": False,
                "outcome": "posted_unverified",
                "verification_required": "screenshot",
                "is_error": True,
                "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
            }
        )
        ax_candidate = BrowserComputerController._safe_ax_candidate_diagnostics(seat_result)
        if ax_candidate:
            result["ax_candidate"] = ax_candidate

    @staticmethod
    def _mark_key_delivery_unverified(result: dict[str, Any]) -> None:
        result.update(
            {
                "executed": True,
                "delivered": True,
                "input_dispatched": True,
                "completion_verified": False,
                "effect_observed": False,
                "postcondition_verified": False,
                "outcome": "posted_unverified",
                "verification_required": "focus_state",
                "is_error": True,
                "error_code": "KEY_EFFECT_NOT_VERIFIED",
            }
        )

    @staticmethod
    def _safe_ax_candidate_diagnostics(seat_result: Any) -> dict[str, Any]:
        if not isinstance(seat_result, dict):
            return {}
        data = seat_result.get("data")
        if not isinstance(data, dict):
            return {}
        candidate = data.get("ax_candidate")
        if not isinstance(candidate, dict):
            return {}
        safe: dict[str, Any] = {}
        for key in (
            "driver_registered",
            "driver_available",
            "background_type_capable",
            "pyobjc_ax_import_available",
            "ax_process_trusted",
            "ax_set_value_unsafe_app",
            "target_app_present",
            "target_bundle_present",
            "target_pid_present",
            "target_window_present",
            "attempted",
        ):
            if isinstance(candidate.get(key), bool):
                safe[key] = candidate[key]
        result_code = str(candidate.get("result_code") or "")
        if result_code in {
            "AX_DRIVER_NOT_REGISTERED",
            "AX_DRIVER_UNAVAILABLE",
            "AX_CAPABILITY_UNAVAILABLE",
            "AX_BACKGROUND_TYPE_UNSUPPORTED",
            "AX_DRIVER_ELIGIBLE",
            "AX_IMPORT_UNAVAILABLE",
            "AX_NOT_TRUSTED",
            "AX_SET_VALUE_UNSAFE_APP",
            "AX_TARGET_MISSING",
            "AX_ELIGIBLE",
            "AX_DIAGNOSTICS_UNAVAILABLE",
            "AX_TYPE_VERIFIED",
            "AX_TYPE_POSTED_UNVERIFIED",
            "AX_TYPE_NOT_EXECUTED",
            "AX_DRIVER_ERROR",
        }:
            safe["result_code"] = result_code
        return safe

    @staticmethod
    def _background_visible_window_required(
        action: str,
        action_payload: dict[str, Any],
        system: str,
        *,
        implicit: bool = False,
        seat_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        app = BrowserComputerController._app_name_from_payload(action_payload)
        visible_target_required = bool(app or action_payload.get("window_id") or action_payload.get("hwnd"))
        if implicit:
            recovery_kind = "foreground_confirmation_required"
        elif visible_target_required:
            recovery_kind = "visible_window_required"
        else:
            recovery_kind = "foreground_fallback_available"
        note = (
            "Show or focus the target app/window if needed, then confirm foreground execution."
            if visible_target_required
            else "Retry with fallback='foreground' only after explicit user confirmation and approval."
        )
        prompt = "backgroundで実行できません。foregroundで作業しますか？"
        reason = (
            "Background mode is the default for this safe action, but no approved background driver could execute it."
            if implicit
            else "Requested background mode could not run safely for this target; use visible windows instead."
        )
        retry_payload = dict(action_payload or {})
        retry_payload.pop("background", None)
        retry_payload["fallback"] = "foreground"
        notes = seat_result.get("notes") if isinstance(seat_result, dict) else None
        result = {
            "action": action,
            "executed": False,
            "is_error": True,
            "platform": system,
            "background": True,
            "reason": reason,
            "message": prompt,
            "user_prompt": prompt,
            "recovery": {
                "kind": recovery_kind,
                "requires_approval": True,
                "note": note,
                "prompt": prompt,
                "retry_payload": retry_payload,
            },
            **({"notes": notes} if notes else {}),
            **({"target_app": app} if app else {}),
        }
        if action == "computer.type" and isinstance(seat_result, dict) and seat_result.get("executed") is True:
            BrowserComputerController._mark_type_delivery_unverified(result, seat_result)
        if action == "computer.key" and isinstance(seat_result, dict) and seat_result.get("executed") is True:
            BrowserComputerController._mark_key_delivery_unverified(result)
        return result

    def _clipboard_read(self, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        include_content = self._truthy(payload.get("include_content")) or self._truthy(payload.get("full_content"))
        approval_payload = self._safe_payload(
            {
                **payload,
                "include_content": include_content,
                "clipboard_access": "full_content" if include_content else "preview_only",
            }
        )
        if not (yolo_mode or self._consume_approval(payload, "computer.clipboard.read", approval_payload)):
            return self._approval_required("computer.clipboard.read", approval_payload)
        content = self._system_clipboard_read()
        result: dict[str, Any] = {
            "action": "computer.clipboard.read",
            "format": "text/plain",
            "content_preview": self._clipboard_preview(content),
            "content_included": include_content,
            "length": len(content),
            "truncated": len(content) > _CLIPBOARD_PREVIEW_CHARS,
        }
        if include_content:
            result["content"] = content
        else:
            result["content_note"] = (
                "Full clipboard content is omitted by default; retry with include_content=true "
                "after explicit approval when the model needs the exact text."
            )
        return result

    def _clipboard_write(self, action: str, payload: dict[str, Any], *, yolo_mode: bool) -> dict[str, Any]:
        content = "" if action == "computer.clipboard.clear" else str(
            payload.get("content", payload.get("text", payload.get("value", ""))) or ""
        )
        approval_payload = self._safe_payload({**payload, "content": content})
        if not (yolo_mode or self._consume_approval(payload, action, approval_payload)):
            return self._approval_required(action, approval_payload)
        self._system_clipboard_write(content)
        return {
            "action": action,
            "written": True,
            "format": "text/plain",
            "length": len(content),
            "cleared": action == "computer.clipboard.clear",
        }

    @staticmethod
    def _system_clipboard_read() -> str:
        system = platform.system()
        if system == "Darwin":
            completed = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
            return completed.stdout
        if system == "Windows":
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                check=False,
            )
            return completed.stdout
        if system == "Linux":
            for command in (["wl-paste"], ["xclip", "-selection", "clipboard", "-out"], ["xsel", "--clipboard", "--output"]):
                if shutil.which(command[0]):
                    completed = subprocess.run(command, capture_output=True, text=True, check=False)
                    if completed.returncode == 0:
                        return completed.stdout
            raise RuntimeError("Linux clipboard requires wl-paste, xclip, or xsel.")
        raise RuntimeError("Clipboard is supported on macOS, Windows, and Linux.")

    @staticmethod
    def _system_clipboard_write(content: str) -> None:
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["pbcopy"], input=content, text=True, check=True)
            return
        if system == "Windows":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=content,
                text=True,
                check=True,
            )
            return
        if system == "Linux":
            for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                if shutil.which(command[0]):
                    subprocess.run(command, input=content, text=True, check=True)
                    return
            raise RuntimeError("Linux clipboard requires wl-copy, xclip, or xsel.")
        raise RuntimeError("Clipboard is supported on macOS, Windows, and Linux.")

    @staticmethod
    def _clipboard_preview(content: str) -> str:
        if len(content) <= _CLIPBOARD_PREVIEW_CHARS:
            return content
        return content[:_CLIPBOARD_PREVIEW_CHARS] + "..."

    @staticmethod
    def _should_capture_after_action(action: str, payload: dict[str, Any]) -> bool:
        if action == "computer.move":
            return payload.get("include_screenshot") is True
        return payload.get("include_screenshot", True) is not False

    def _desktop_approval_payload(
        self,
        action: str,
        payload: dict[str, Any],
        action_payload: dict[str, Any],
        *,
        virtual_only: bool,
    ) -> dict[str, Any]:
        approval_payload = self._safe_payload(payload)
        if action in {"computer.move", "computer.click"}:
            approval_payload["virtual_only"] = bool(virtual_only)
            approval_payload["resolved_coordinates"] = {
                "x": int(action_payload.get("x", 0)),
                "y": int(action_payload.get("y", 0)),
            }
        elif action == "computer.drag":
            approval_payload["virtual_only"] = bool(virtual_only)
            approval_payload["resolved_coordinates"] = {
                "from": {
                    "x": int(action_payload.get("x1", action_payload.get("x", 0))),
                    "y": int(action_payload.get("y1", action_payload.get("y", 0))),
                },
                "to": {
                    "x": int(action_payload.get("x2", action_payload.get("x", 0))),
                    "y": int(action_payload.get("y2", action_payload.get("y", 0))),
                },
            }
        return approval_payload

    def _pointer_action_is_virtual_only(self, action: str, payload: dict[str, Any]) -> bool:
        if action not in {"computer.move", "computer.click", "computer.drag"}:
            return False
        if self._truthy(payload.get("physical")):
            return False
        return True

    @staticmethod
    def _background_requested(payload: dict[str, Any]) -> bool:
        if payload.get("background") is True:
            return True
        mode = str(payload.get("mode") or payload.get("method") or payload.get("driver") or "").strip().lower()
        return mode in {"background", "browser_background", "chromium_background", "chrome_background", "chrome_background_dom", "background_dom"}

    @staticmethod
    def _foreground_fallback_requested(payload: dict[str, Any]) -> bool:
        if payload.get("foreground") is True:
            return True
        mode = str(
            payload.get("fallback")
            or payload.get("mode")
            or payload.get("method")
            or payload.get("driver")
            or ""
        ).strip().lower()
        return mode in {"foreground", "foreground_input", "visible", "visible_foreground"}

    @staticmethod
    def _app_name_from_payload(payload: dict[str, Any]) -> str:
        return str(
            payload.get("app")
            or payload.get("application")
            or payload.get("target_app")
            or payload.get("browser")
            or payload.get("browser_app")
            or ""
        ).strip()

    def _focus_action_target(self, payload: dict[str, Any]) -> bool:
        if payload.get("focus") is False:
            return False
        filters = self._window_filter(payload)
        app = filters.get("app", "").lower()
        title = filters.get("title", "").lower()
        selected = self._capture_target(payload)
        if selected and self._window_matches_filter(selected, app=app, title=title):
            active = self._active_window()
            if active and self._window_records_match(active, selected):
                return True
            # Browser chrome can expose an address-bar or transient chrome
            # surface as the active AX window while the owning page remains
            # the selected content window. Do not refocus the content window
            # here: doing so would discard the address-bar focus established
            # by the immediately preceding shortcut. The helper below is
            # deliberately same-PID and shape constrained.
            action = self._foreground_input_action_from_payload(payload)
            if self._app_only_foreground_input_matches_chrome_surface(action, payload, active, selected):
                return True
            self._focus_window(selected)
            return True
        if app or title:
            for item in self._list_windows():
                window = self._normalize_window_record(item)
                if window and self._is_usable_target_window(window) and self._window_matches_filter(window, app=app, title=title):
                    self._focus_window(window)
                    return True
        if app and self._activate_app_name(filters.get("app", "")):
            return True
        return False

    @staticmethod
    def _foreground_input_action_from_payload(payload: dict[str, Any]) -> str:
        if "text" in payload:
            return "computer.type"
        if any(key in payload for key in ("key", "key_combo", "modifiers", "modifier")):
            return "computer.key"
        if any(key in payload for key in ("direction", "amount", "clicks", "delta_x", "delta_y")):
            return "computer.scroll"
        return ""

    def _foreground_action_focus_error(self, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if action not in {"computer.type", "computer.key", "computer.scroll", "computer.click", "computer.drag"}:
            return None
        if action in {"computer.click", "computer.drag"} and payload.get("physical") is not True:
            return None
        target = self._foreground_action_target(payload)
        if target is None:
            return None
        active = self._active_window()
        if active and self._window_records_match(active, target):
            return None
        if (
            action == "computer.click"
            and active
            and self._same_app_window(active, target)
            and self._action_point_inside_window(payload, active)
        ):
            return None
        self._focus_window(target)
        time.sleep(0.2)
        active = self._active_window()
        if active and self._window_records_match(active, target):
            return None
        if (
            action == "computer.click"
            and active
            and self._same_app_window(active, target)
            and self._action_point_inside_window(payload, active)
        ):
            return None
        filters = self._window_filter(payload)
        target_window_id = self._window_id_int(target.get("window_id"))
        if not target_window_id and active and self._window_matches_filter(
            active,
            app=filters.get("app", "").lower(),
            title=filters.get("title", "").lower(),
        ):
            return None
        if self._app_only_foreground_input_matches_chrome_surface(action, payload, active, target):
            return None
        if self._app_only_physical_click_matches_chrome_surface(action, payload, active, target):
            return None
        result = {
            "action": action,
            "executed": False,
            "is_error": True,
            "platform": platform.system(),
            "reason": "Foreground input target is not active. Refusing to type, press keys, scroll, or physically click into the wrong app.",
            "active_window": active,
            "selected_window": target,
            "recovery": {
                "kind": "focus_required",
                "note": "Bring the selected app/window to the foreground, then retry the foreground input action.",
            },
        }
        if action == "computer.type":
            result["diagnostics"] = {
                "error_code": "TYPE_FOREGROUND_TARGET_NOT_VERIFIED",
                "input_strategy": "none",
                "completion_verified": False,
                "input_dispatched": False,
                "dispatched_units": 0,
                "target_pid_stable": False,
                "focused_element_stable": False,
                "failure_stage": "foreground_target_verification",
                "direct_ax_attempted": False,
                "mutation_observed": False,
            }
        return result

    def _app_only_foreground_input_matches_chrome_surface(
        self,
        action: str,
        payload: dict[str, Any],
        active: dict[str, Any] | None,
        target: dict[str, Any],
    ) -> bool:
        if action not in {"computer.type", "computer.key", "computer.scroll"}:
            return False
        return self._app_only_payload_matches_same_app_chrome_surface(payload, active, target)

    def _app_only_physical_click_matches_chrome_surface(
        self,
        action: str,
        payload: dict[str, Any],
        active: dict[str, Any] | None,
        target: dict[str, Any],
    ) -> bool:
        if action != "computer.click" or payload.get("physical") is not True:
            return False
        if not self._app_only_payload_matches_same_app_chrome_surface(payload, active, target):
            return False
        return self._action_point_inside_window(payload, target)

    def _app_only_payload_matches_same_app_chrome_surface(
        self,
        payload: dict[str, Any],
        active: dict[str, Any] | None,
        target: dict[str, Any],
    ) -> bool:
        if not active:
            return False
        filters = self._window_filter(payload)
        if not filters.get("app") or filters.get("title"):
            return False
        if isinstance(payload.get("window"), dict):
            return False
        if self._optional_int(payload.get("pid")) is not None:
            return False
        if self._window_id_int(payload.get("window_id")) or self._window_id_int(payload.get("hwnd")):
            return False
        if not self._same_app_window(active, target):
            return False
        active_pid = self._optional_int(active.get("pid"))
        target_pid = self._optional_int(target.get("pid"))
        if active_pid is None or target_pid is None or active_pid != target_pid:
            return False
        # macOS can report a browser chrome/accessibility surface as the active
        # window while the content window remains the selected target. For an
        # app-only keyboard action, same-app foreground is the safety boundary.
        active_title = str(active.get("title") or "").strip()
        if active_title:
            return False
        try:
            active_height = int(active.get("height") or 0)
            target_height = int(target.get("height") or 0)
        except (TypeError, ValueError):
            return False
        try:
            active_width = int(active.get("width") or 0)
            active_x = int(active.get("x") or 0)
            active_y = int(active.get("y") or 0)
            target_width = int(target.get("width") or 0)
            target_x = int(target.get("x") or 0)
            target_y = int(target.get("y") or 0)
        except (TypeError, ValueError):
            return False
        if active_height <= 0 or active_width <= 0:
            return False
        if target_height > 0 and active_height >= target_height:
            return False
        # Full-width, shallow untitled surfaces are browser toolbars/address
        # bars already covered by the original contract.
        if active_height <= 96:
            return True
        # ChatGPT Atlas exposes its translation chrome as an untitled 346x113
        # auxiliary AX window. Accept only this tightly bounded, same-PID,
        # in-window chrome class; ordinary dialogs and other content windows
        # remain rejected.
        target_app = str(target.get("app") or "").strip().lower()
        if target_app != "chatgpt atlas" or active_width > 480 or active_height > 160:
            return False
        if target_width <= 0 or target_height <= 0:
            return False
        return (
            active_x >= target_x
            and active_y >= target_y
            and active_x + active_width <= target_x + target_width
            and active_y + active_height <= target_y + target_height
        )

    @classmethod
    def _same_app_window(cls, active: dict[str, Any], target: dict[str, Any]) -> bool:
        active_app = str(active.get("app") or "").strip().lower()
        target_app = str(target.get("app") or "").strip().lower()
        if not (active_app and target_app):
            return False
        return cls._app_name_matches(target_app, active_app)

    @staticmethod
    def _action_point_inside_window(payload: dict[str, Any], window: dict[str, Any]) -> bool:
        try:
            x = int(payload.get("x"))
            y = int(payload.get("y"))
            left = int(window.get("x"))
            top = int(window.get("y"))
            width = int(window.get("width"))
            height = int(window.get("height"))
        except (TypeError, ValueError):
            return False
        if width <= 0 or height <= 0:
            return False
        return left <= x <= left + width - 1 and top <= y <= top + height - 1

    def _foreground_action_target(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        selected = self._capture_target(payload)
        if selected and self._is_usable_target_window(selected):
            return selected
        if self._has_window_filter(payload):
            selected = self._matching_window(payload)
            if selected and self._is_usable_target_window(selected):
                return selected
        return None

    @classmethod
    def _window_records_match(cls, active: dict[str, Any], target: dict[str, Any]) -> bool:
        active_id = cls._window_id_int(active.get("window_id"))
        target_id = cls._window_id_int(target.get("window_id"))
        if active_id and target_id:
            return active_id == target_id
        target_app = str(target.get("app") or "").strip().lower()
        target_title = str(target.get("title") or "").strip().lower()
        if not (target_app or target_title):
            return False
        return cls._window_matches_filter(active, app=target_app, title=target_title)

    @classmethod
    def _window_matches_filter(cls, window: dict[str, Any], *, app: str = "", title: str = "") -> bool:
        item_app = str(window.get("app") or "").lower()
        item_title = str(window.get("title") or "").lower()
        if app and not cls._app_name_matches(app, item_app):
            # Some Windows window enumerators omit the process/app label even though
            # the browser name is still present in the window title.
            if item_app or not cls._app_name_matches(app, item_title):
                return False
        if title and title not in item_title:
            return False
        return True

    @classmethod
    def _window_matches_explicit_filter(cls, window: dict[str, Any], payload: dict[str, Any]) -> bool:
        filters = cls._window_filter(payload)
        if not cls._window_matches_filter(
            window,
            app=filters.get("app", "").lower(),
            title=filters.get("title", "").lower(),
        ):
            return False
        wanted_pid = cls._optional_int(payload.get("pid"))
        if wanted_pid is not None and int(window.get("pid") or 0) != wanted_pid:
            return False
        wanted_window_id = cls._window_id_int(payload.get("window_id"))
        if wanted_window_id:
            window_id = cls._window_id_int(window.get("window_id"))
            frame_ids = window.get("frame_window_ids") if isinstance(window.get("frame_window_ids"), list) else []
            if window_id != wanted_window_id and wanted_window_id not in frame_ids:
                return False
        wanted_hwnd = cls._window_id_int(payload.get("hwnd"))
        if wanted_hwnd and cls._window_id_int(window.get("hwnd")) != wanted_hwnd:
            return False
        return True

    def _capture_action_result_screenshot(
        self,
        payload: dict[str, Any],
        marker: dict[str, Any] | None,
        *,
        action_name: str = "computer.click",
        drag_marker: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        action_slug = action_name.split(".")[-1].replace("_", "-") or "action"
        path = self._artifact_root / f"{action_slug}-{int(time.time() * 1000)}.png"
        capture = self._capture_screenshot(path, payload)
        if not capture.get("supported", True):
            return {}
        crop_result = self._apply_screenshot_crop(path, payload, capture)
        crop_reference = crop_result.get("crop_reference") if crop_result else None
        action_target = crop_result.get("action_target") if crop_result else capture.get("action_coordinate_system")
        if crop_result and isinstance(crop_result.get("path"), Path):
            path = crop_result["path"]
        model_path = self._model_screenshot_copy(path)
        system = capture.get("platform", platform.system())
        result = self._screenshot_result(
            path,
            model_path,
            system,
            capture_target=capture.get("target_window"),
            action_target=action_target,
            crop_reference=crop_reference,
        )
        marked_model_path = self._marker_preview_image(model_path, result, marker=marker, drag_marker=drag_marker)
        if marked_model_path:
            result["unmarked_model_image_path"] = str(model_path)
            model_path = marked_model_path
        result["action"] = action_name
        result["screenshot_path"] = str(path)
        result["model_image_path"] = str(model_path)
        result["verification"] = {
            "kind": "post_action_screenshot",
            "note": "Inspect this screenshot to verify the visible UI changed as intended.",
        }
        data_url = self._image_data_url(model_path)
        if data_url:
            result["data_url"] = data_url
            result["model_image"] = data_url
        feedback_type = ""
        if action_name == "computer.move":
            feedback_type = "post_move_screenshot"
        elif action_name == "computer.click":
            feedback_type = "post_click_screenshot"
        if feedback_type:
            visual_feedback = {
                "type": feedback_type,
                "screenshot_path": str(path),
                "model_image_path": str(model_path),
            }
            if data_url:
                visual_feedback["data_url"] = data_url
            if marker:
                visual_feedback["marker"] = marker
            if drag_marker:
                visual_feedback["drag_marker"] = drag_marker
            result["visual_feedback"] = visual_feedback
        if marker:
            result["click_marker"] = marker
        if drag_marker:
            result["drag_marker"] = drag_marker
        self._remember_last_screenshot(result)
        return result

    def _capture_or_reuse_screenshot(self, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        crop_payload = self._crop_payload(payload)
        source = crop_payload.get("source") if isinstance(crop_payload, dict) else None
        source_name = str(source or payload.get("source") or "").strip().lower()
        use_latest = source_name in {
            "latest",
            "last",
            "last_screenshot",
            "latest_screenshot",
            "current",
            "current_crop",
            "latest_crop",
            "last_crop",
            "attached",
            "attached_image",
        }
        if use_latest:
            state = self._computer_state()
            last, source_role = self._screenshot_reuse_source(state, source_name, crop_payload=bool(crop_payload))
            source_path = Path(str(last.get("path") or ""))
            if source_path.exists():
                if not crop_payload:
                    try:
                        shutil.copyfile(source_path, path)
                    except Exception:
                        path = source_path
                return {
                    "platform": platform.system(),
                    "target_window": last.get("target_window") if isinstance(last.get("target_window"), dict) else None,
                    "source_path": source_path,
                    "source_role": source_role,
                    "source_is_crop": isinstance(last.get("crop_reference"), dict),
                    "source_image_size": last.get("image_size") if isinstance(last.get("image_size"), dict) else None,
                    "source_action_coordinate_system": (
                        last.get("action_coordinate_system")
                        if isinstance(last.get("action_coordinate_system"), dict)
                        else None
                    ),
                }
        return self._capture_screenshot(path, payload)

    @staticmethod
    def _screenshot_reuse_source(
        state: dict[str, Any],
        source_name: str,
        *,
        crop_payload: bool,
    ) -> tuple[dict[str, Any], str]:
        current_crop_sources = {"current", "current_crop", "latest_crop", "last_crop", "attached", "attached_image"}
        if crop_payload and source_name not in current_crop_sources:
            full = state.get("last_full_screenshot") if isinstance(state.get("last_full_screenshot"), dict) else {}
            if full.get("path"):
                return full, "last_full_screenshot"
        last = state.get("last_screenshot") if isinstance(state.get("last_screenshot"), dict) else {}
        return last, "last_screenshot"

    def _apply_screenshot_crop(self, path: Path, payload: dict[str, Any], capture: dict[str, Any]) -> dict[str, Any] | None:
        crop_payload = self._crop_payload(payload)
        if not crop_payload:
            return None
        source_path = capture.get("source_path") if isinstance(capture.get("source_path"), Path) else path
        if not source_path.exists():
            return None
        source_size = self._image_size(source_path)
        if not source_size:
            return None
        box = self._crop_box(crop_payload, source_size)
        if not box:
            return None
        crop_path = path.with_name(path.stem + "-crop.png")
        if not self._crop_png(source_path, crop_path, box):
            return None
        source_action = capture.get("source_action_coordinate_system")
        if not isinstance(source_action, dict):
            source_action = self._action_coordinate_system(platform.system(), source_size, capture_target=capture.get("target_window"))
        action_target = self._crop_action_target(box, source_size, source_action)
        left, top, right, bottom = box
        crop_reference = {
            "source": "latest_screenshot" if capture.get("source_path") else "captured_screenshot",
            "source_path": str(source_path),
            "source_image_size": {"width": source_size[0], "height": source_size[1]},
            "box": {"x": left, "y": top, "width": right - left + 1, "height": bottom - top + 1},
            "coordinate_space": "screenshot_image",
            "source_role": capture.get("source_role") or ("latest_screenshot" if capture.get("source_path") else "captured_screenshot"),
            "source_is_crop": bool(capture.get("source_is_crop")),
            "source_action_coordinate_system": source_action,
            "action_box": action_target,
        }
        return {"path": crop_path, "crop_reference": crop_reference, "action_target": action_target}

    @staticmethod
    def _crop_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        for key in ("crop", "zoom", "crop_box", "zoom_box"):
            value = payload.get(key)
            if isinstance(value, dict):
                crop = BrowserComputerController._normalized_crop_payload(dict(value))
                for inherited_key in ("source", "coordinate_space", "space"):
                    if inherited_key in payload and inherited_key not in crop:
                        crop[inherited_key] = payload.get(inherited_key)
                return crop
            if isinstance(value, (list, tuple)) and len(value) >= 4:
                return {"box": list(value), "coordinate_space": payload.get("coordinate_space") or payload.get("space")}
        zoom = payload.get("zoom")
        if isinstance(zoom, (int, float, str)) and str(zoom).strip():
            return {
                "zoom_factor": zoom,
                "x": payload.get("normalized_x", payload.get("x", 500)),
                "y": payload.get("normalized_y", payload.get("y", 500)),
                "coordinate_space": payload.get("coordinate_space") or payload.get("space") or "normalized_1000",
                "source": payload.get("source"),
            }
        if any(key in payload for key in ("crop_x", "crop_y", "crop_width", "crop_height")):
            return BrowserComputerController._normalized_crop_payload({
                "x": payload.get("crop_x"),
                "y": payload.get("crop_y"),
                "width": payload.get("crop_width"),
                "height": payload.get("crop_height"),
                "coordinate_space": payload.get("coordinate_space") or payload.get("space"),
                "source": payload.get("source"),
            })
        return None

    @staticmethod
    def _normalized_crop_payload(crop: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(crop)
        aliases = {
            "crop_x": "x",
            "crop_y": "y",
            "crop_width": "width",
            "crop_height": "height",
        }
        for alias, target in aliases.items():
            if target not in normalized and alias in normalized:
                normalized[target] = normalized.get(alias)
        return normalized

    def _crop_box(self, crop: dict[str, Any], image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
        width, height = image_size
        if width <= 0 or height <= 0:
            return None
        space = str(crop.get("coordinate_space") or crop.get("space") or "").strip().lower()
        normalized = space in self._normalized_coordinate_spaces() or any(
            key in crop for key in ("normalized_x", "normalized_y", "normalized_width", "normalized_height", "normalized_box")
        )
        if crop.get("zoom_factor") is not None:
            factor = max(1.0, self._numeric_coordinate(crop.get("zoom_factor"), 1))
            if factor <= 1.0:
                return None
            center_x_value = crop.get("normalized_x") if normalized and crop.get("normalized_x") is not None else crop.get("x", 500 if normalized else width / 2)
            center_y_value = crop.get("normalized_y") if normalized and crop.get("normalized_y") is not None else crop.get("y", 500 if normalized else height / 2)
            center_x = self._normalized_to_pixel(center_x_value, width) if normalized else int(round(self._numeric_coordinate(center_x_value, width / 2)))
            center_y = self._normalized_to_pixel(center_y_value, height) if normalized else int(round(self._numeric_coordinate(center_y_value, height / 2)))
            crop_width = max(2, int(round(width / factor)))
            crop_height = max(2, int(round(height / factor)))
            left = center_x - crop_width // 2
            top = center_y - crop_height // 2
            right = left + crop_width - 1
            bottom = top + crop_height - 1
            if left < 0:
                right -= left
                left = 0
            if top < 0:
                bottom -= top
                top = 0
            if right >= width:
                shift = right - width + 1
                left = max(0, left - shift)
                right = width - 1
            if bottom >= height:
                shift = bottom - height + 1
                top = max(0, top - shift)
                bottom = height - 1
            return left, top, right, bottom
        width_height_box = False
        box = crop.get("normalized_box") if normalized and crop.get("normalized_box") is not None else crop.get("box")
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            left, top, right_or_width, bottom_or_height = [self._numeric_coordinate(item) for item in box[:4]]
            if crop.get("box_format") in {"xywh", "x_y_width_height"} or crop.get("width_height") is True:
                right = left + right_or_width
                bottom = top + bottom_or_height
                width_height_box = True
            else:
                right = right_or_width
                bottom = bottom_or_height
        else:
            left = self._numeric_coordinate(crop.get("normalized_x") if normalized else crop.get("x", crop.get("left", 0)))
            top = self._numeric_coordinate(crop.get("normalized_y") if normalized else crop.get("y", crop.get("top", 0)))
            crop_width = crop.get("normalized_width") if normalized else crop.get("width")
            crop_height = crop.get("normalized_height") if normalized else crop.get("height")
            if crop_width is not None and crop_height is not None:
                right = left + self._numeric_coordinate(crop_width)
                bottom = top + self._numeric_coordinate(crop_height)
            else:
                right = self._numeric_coordinate(crop.get("normalized_right") if normalized else crop.get("right"), left)
                bottom = self._numeric_coordinate(crop.get("normalized_bottom") if normalized else crop.get("bottom"), top)
        if normalized:
            left = self._normalized_to_pixel(left, width)
            top = self._normalized_to_pixel(top, height)
            right = self._normalized_to_pixel(right, width)
            bottom = self._normalized_to_pixel(bottom, height)
        else:
            left = int(round(left))
            top = int(round(top))
            right = int(round(right - 1 if width_height_box or crop.get("width") is not None or crop.get("height") is not None else right))
            bottom = int(round(bottom - 1 if width_height_box or crop.get("width") is not None or crop.get("height") is not None else bottom))
        left = max(0, min(int(left), width - 1))
        right = max(0, min(int(right), width - 1))
        top = max(0, min(int(top), height - 1))
        bottom = max(0, min(int(bottom), height - 1))
        if right < left:
            left, right = right, left
        if bottom < top:
            top, bottom = bottom, top
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    @classmethod
    def _normalized_to_pixel(cls, value: Any, size: int) -> int:
        normalized = max(0.0, min(1000.0, cls._numeric_coordinate(value)))
        return int(round(normalized * max(size - 1, 0) / 1000.0))

    def _crop_action_target(
        self,
        box: tuple[int, int, int, int],
        source_size: tuple[int, int],
        source_action: dict[str, Any] | None,
    ) -> dict[str, Any]:
        left, top, right, bottom = box
        fallback = {"origin": "top_left", "unit": "px", "screen": "cropped", "x": 0, "y": 0, "width": right - left + 1, "height": bottom - top + 1}
        if not isinstance(source_action, dict):
            return fallback
        try:
            source_width = max(int(source_size[0]), 1)
            source_height = max(int(source_size[1]), 1)
            action_x = int(source_action.get("x", 0))
            action_y = int(source_action.get("y", 0))
            action_width = int(source_action.get("width", 0))
            action_height = int(source_action.get("height", 0))
        except Exception:
            return fallback
        if action_width <= 0 or action_height <= 0:
            return fallback
        crop_x = action_x + round(left * max(action_width - 1, 0) / max(source_width - 1, 1))
        crop_y = action_y + round(top * max(action_height - 1, 0) / max(source_height - 1, 1))
        crop_right = action_x + round(right * max(action_width - 1, 0) / max(source_width - 1, 1))
        crop_bottom = action_y + round(bottom * max(action_height - 1, 0) / max(source_height - 1, 1))
        target = dict(source_action)
        target.update(
            {
                "x": int(crop_x),
                "y": int(crop_y),
                "width": max(int(crop_right - crop_x + 1), 1),
                "height": max(int(crop_bottom - crop_y + 1), 1),
                "screen": "crop_of_" + str(source_action.get("screen") or "screenshot"),
            }
        )
        target["x_range"] = [target["x"], target["x"] + max(target["width"] - 1, 0)]
        target["y_range"] = [target["y"], target["y"] + max(target["height"] - 1, 0)]
        return target

    def _capture_screenshot(self, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        system = platform.system()
        target = self._capture_target(payload)
        explicit_desktop = str(payload.get("target") or payload.get("capture_target") or "").strip().lower() in {
            "primary_display",
            "all_displays",
            "screen",
            "display",
            "desktop",
        }
        if target is None and self._has_window_filter(payload) and not explicit_desktop:
            return {
                "platform": system,
                "supported": False,
                "reason": "No visible window matched the requested app/title; refusing to capture the front desktop because it would mislead the model.",
                "target_filter": self._window_filter(payload),
            }
        if system == "Darwin":
            swift_capture = self._darwin_swift_screenshot(path, payload, target)
            if swift_capture is not None:
                return swift_capture
            if target:
                capture_rect = self._normalize_rect(target.get("capture_rect"))
                if capture_rect:
                    rect = "{},{},{},{}".format(
                        int(capture_rect.get("x", 0)),
                        int(capture_rect.get("y", 0)),
                        int(capture_rect.get("width", 0)),
                        int(capture_rect.get("height", 0)),
                    )
                    try:
                        subprocess.run(
                            ["screencapture", "-x", "-R", rect, str(path)],
                            check=True,
                            timeout=_DARWIN_SCREENSHOT_TIMEOUT_SECONDS,
                        )
                    except subprocess.CalledProcessError:
                        window_id = target.get("window_id")
                        if not window_id:
                            raise
                        subprocess.run(
                            ["screencapture", "-x", "-l", str(int(window_id)), str(path)],
                            check=True,
                            timeout=_DARWIN_SCREENSHOT_TIMEOUT_SECONDS,
                        )
                else:
                    window_id = target.get("window_id")
                    if window_id:
                        subprocess.run(
                            ["screencapture", "-x", "-l", str(int(window_id)), str(path)],
                            check=True,
                            timeout=_DARWIN_SCREENSHOT_TIMEOUT_SECONDS,
                        )
                    else:
                        rect = "{},{},{},{}".format(
                            int(target.get("x", 0)),
                            int(target.get("y", 0)),
                            int(target.get("width", 0)),
                            int(target.get("height", 0)),
                        )
                        subprocess.run(
                            ["screencapture", "-x", "-R", rect, str(path)],
                            check=True,
                            timeout=_DARWIN_SCREENSHOT_TIMEOUT_SECONDS,
                        )
            else:
                subprocess.run(
                    ["screencapture", "-x", str(path)],
                    check=True,
                    timeout=_DARWIN_SCREENSHOT_TIMEOUT_SECONDS,
                )
            return {"platform": system, "target_window": target}
        if system == "Windows":
            capture_bounds = self._windows_screenshot(path, target=target)
            action_coordinate_system = None
            if isinstance(capture_bounds, dict):
                action_coordinate_system = self._action_coordinate_system(
                    system,
                    (int(capture_bounds.get("width", 0)), int(capture_bounds.get("height", 0))),
                    capture_target=capture_bounds,
                )
            return {
                "platform": system,
                "target_window": target,
                "action_coordinate_system": action_coordinate_system,
            }
        if system == "Linux":
            try:
                from ..computer.linux import xdotool

                capture = xdotool.screenshot(path, target=target)
                if capture.get("path"):
                    return {
                        "platform": system,
                        "target_window": target,
                        "action_coordinate_system": self._action_coordinate_system(
                            system,
                            (int(capture.get("width", 0)), int(capture.get("height", 0))),
                            capture_target=target,
                        ),
                    }
                return {
                    "platform": system,
                    "supported": False,
                    "reason": capture.get("error") or "Linux screenshot capture failed.",
                    "target_window": target,
                }
            except Exception as exc:
                return {"platform": system, "supported": False, "reason": str(exc), "target_window": target}
        return {"platform": system, "supported": False}

    def _darwin_swift_screenshot(
        self,
        path: Path,
        payload: dict[str, Any],
        target: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        try:
            from ..computer.mac.swift_host import MacSwiftComputerHost

            host = MacSwiftComputerHost()
            if not host.available():
                return None
            args = dict(payload or {})
            args["output_path"] = str(path)
            if isinstance(target, dict):
                for key in ("window_id", "pid", "app", "title", "x", "y", "width", "height"):
                    if target.get(key) is not None:
                        args.setdefault(key, target.get(key))
            result = host.run("computer.screenshot", args)
            if result.get("is_error") or not path.exists():
                return None
            action_coordinate_system = self._action_coordinate_system(
                "Darwin",
                (int(result.get("width", 0)), int(result.get("height", 0))),
                capture_target=result.get("target_window") if isinstance(result.get("target_window"), dict) else target,
            )
            return {
                "platform": "Darwin",
                "target_window": result.get("target_window") if isinstance(result.get("target_window"), dict) else target,
                "action_coordinate_system": action_coordinate_system,
                "driver": "mac_swift_host",
            }
        except Exception:
            return None

    def _capture_target(self, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        payload = payload or {}
        target = str(payload.get("target") or payload.get("capture_target") or "").strip().lower()
        if target in {"primary_display", "all_displays", "screen", "display", "desktop"}:
            return None
        if target in {"active_window", "front_window"}:
            active = self._active_window()
            if active and self._is_usable_target_window(active):
                state = self._computer_state()
                state["target_window"] = active
                self._write_computer_state(state)
            return active
        if isinstance(payload.get("window"), dict):
            selected = self._normalize_window_record(payload.get("window"))
            return selected if self._is_usable_target_window(selected) else None
        if self._has_window_filter(payload):
            selected = self._matching_window(payload)
            if selected is not None:
                state = self._computer_state()
                state["target_window"] = selected
                self._write_computer_state(state)
                return selected
            return None
        state = self._computer_state()
        selected = state.get("target_window") if self._state_matches_artifact_root(state) else None
        if target in {"selected_window", "window", "app"} or (not target and isinstance(selected, dict)):
            selected = self._normalize_window_record(selected)
            if self._is_usable_target_window(selected):
                return selected
            self._clear_target_window()
        return None

    @staticmethod
    def _window_filter(payload: dict[str, Any] | None = None) -> dict[str, str]:
        payload = payload or {}
        app = str(payload.get("app") or payload.get("application") or "").strip()
        title = str(payload.get("title") or payload.get("window_title") or payload.get("title_contains") or "").strip()
        return {"app": app, "title": title}

    def _has_window_filter(self, payload: dict[str, Any] | None = None) -> bool:
        return self._has_explicit_window_filter(payload)

    def _has_explicit_window_filter(self, payload: dict[str, Any] | None = None) -> bool:
        payload = payload or {}
        filters = self._window_filter(payload)
        if filters.get("app") or filters.get("title"):
            return True
        if isinstance(payload.get("window"), dict):
            return True
        for key in ("pid", "window_id", "hwnd"):
            if payload.get(key) not in (None, ""):
                return True
        return False

    def _explicit_target_has_self_contained_window(self, payload: dict[str, Any]) -> bool:
        window = self._normalize_window_record(payload.get("window")) if isinstance(payload.get("window"), dict) else None
        return bool(window and self._window_matches_explicit_filter(window, payload))

    def _matching_window(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        filters = self._window_filter(payload)
        app = filters.get("app", "").lower()
        title = filters.get("title", "").lower()
        explicit_window = self._normalize_window_record(payload.get("window")) if isinstance(payload.get("window"), dict) else None
        if explicit_window and self._window_matches_explicit_filter(explicit_window, payload):
            return explicit_window
        candidates: list[dict[str, Any]] = []
        for item in self._list_windows():
            window = self._normalize_window_record(item)
            if window and self._is_usable_target_window(window) and self._window_matches_explicit_filter(window, payload):
                candidates.append(window)
        selected_candidate = self._best_window_candidate(candidates, app=app, title=title)
        if selected_candidate:
            return selected_candidate
        if self._has_explicit_window_filter(payload):
            return None
        state = self._computer_state()
        selected = self._normalize_window_record(
            state.get("target_window") if self._state_matches_artifact_root(state) else None
        )
        if selected and self._is_usable_target_window(selected) and self._window_matches_explicit_filter(selected, payload):
            return selected
        return None

    def _resolve_action_point(
        self,
        payload: dict[str, Any],
        *,
        infer_window: bool = False,
        remember_cursor: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        state = self._computer_state()
        if not self._state_matches_artifact_root(state):
            state = {}
        cursor = state.get("ai_cursor") if isinstance(state.get("ai_cursor"), dict) else {}
        x, y = self._point_from_payload(payload, cursor)
        target = self._capture_target(payload)
        if target is None and infer_window:
            target = self._window_at_point(x, y)
            if target is not None and remember_cursor:
                state["target_window"] = target
                self._write_computer_state(state)
        coordinate_space = str(payload.get("coordinate_space") or payload.get("space") or "auto").strip().lower()
        if coordinate_space in self._normalized_coordinate_spaces() or self._has_normalized_point(payload):
            normalized_payload, normalized_marker = self._resolve_normalized_point(payload, x, y, state, target=target)
            if normalized_payload is not None:
                if remember_cursor:
                    self._set_ai_cursor(normalized_payload)
                return normalized_payload, normalized_marker
        if coordinate_space in {"model", "model_image", "preview", "screenshot_preview"} or (
            coordinate_space == "auto" and self._point_looks_like_model_coordinate(x, y, state)
        ):
            model_payload, model_marker = self._resolve_model_point(payload, x, y, state)
            if model_payload is not None:
                if remember_cursor:
                    self._set_ai_cursor(model_payload)
                return model_payload, model_marker
        use_window_space = False
        if target and coordinate_space in {"auto", "window", "target", "screenshot", "image"}:
            width = int(target.get("width", 0))
            height = int(target.get("height", 0))
            use_window_space = 0 <= x <= max(width, 0) and 0 <= y <= max(height, 0)
        action_payload = dict(payload)
        if target and use_window_space:
            screen_x = int(target.get("x", 0)) + x
            screen_y = int(target.get("y", 0)) + y
            action_payload["x"] = screen_x
            action_payload["y"] = screen_y
            action_payload["coordinate_space"] = "screen"
            marker = {
                "x": x,
                "y": y,
                "screen_x": screen_x,
                "screen_y": screen_y,
                "coordinate_space": "screenshot_image",
            }
        else:
            action_payload["x"] = x
            action_payload["y"] = y
            action_payload["coordinate_space"] = "screen"
            marker = {"x": x, "y": y, "screen_x": x, "screen_y": y, "coordinate_space": "screen"}
            if target:
                marker["x"] = x - int(target.get("x", 0))
                marker["y"] = y - int(target.get("y", 0))
                marker["coordinate_space"] = "screenshot_image"
        if remember_cursor:
            self._set_ai_cursor(action_payload)
        return action_payload, marker

    @staticmethod
    def _normalized_coordinate_spaces() -> set[str]:
        return {
            "normalized",
            "normalized_1000",
            "browser_tool_test",
            "viewport_normalized",
            "image_normalized",
            "screenshot_normalized",
        }

    @staticmethod
    def _numeric_coordinate(value: Any, default: Any = 0) -> float:
        try:
            return float(value)
        except Exception:
            try:
                return float(default)
            except Exception:
                return 0.0

    @classmethod
    def _point_from_payload(cls, payload: dict[str, Any], cursor: dict[str, Any]) -> tuple[float, float]:
        point = payload.get("point") or payload.get("coordinate") or payload.get("coordinates")
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            order = str(payload.get("point_order") or payload.get("coordinate_order") or "yx").replace(",", "").strip().lower()
            first = cls._numeric_coordinate(point[0])
            second = cls._numeric_coordinate(point[1])
            if order in {"xy", "x-y", "x_then_y"}:
                return first, second
            return second, first
        if isinstance(point, dict):
            return (
                cls._numeric_coordinate(point.get("x"), cursor.get("x", 0)),
                cls._numeric_coordinate(point.get("y"), cursor.get("y", 0)),
            )
        return (
            cls._numeric_coordinate(payload.get("x"), cursor.get("x", 0)),
            cls._numeric_coordinate(payload.get("y"), cursor.get("y", 0)),
        )

    @staticmethod
    def _has_normalized_point(payload: dict[str, Any]) -> bool:
        return any(key in payload for key in ("normalized_point", "normalized_x", "normalized_y"))

    def _resolve_normalized_point(
        self,
        payload: dict[str, Any],
        x: float,
        y: float,
        state: dict[str, Any],
        *,
        target: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        point = payload.get("normalized_point")
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            y = self._numeric_coordinate(point[0])
            x = self._numeric_coordinate(point[1])
        elif isinstance(point, dict):
            x = self._numeric_coordinate(point.get("x"), x)
            y = self._numeric_coordinate(point.get("y"), y)
        if payload.get("normalized_x") is not None:
            x = self._numeric_coordinate(payload.get("normalized_x"), x)
        if payload.get("normalized_y") is not None:
            y = self._numeric_coordinate(payload.get("normalized_y"), y)
        x = max(0.0, min(1000.0, self._numeric_coordinate(x)))
        y = max(0.0, min(1000.0, self._numeric_coordinate(y)))

        last = state.get("last_screenshot") if isinstance(state, dict) else None
        action_space = last.get("action_coordinate_system") if isinstance(last, dict) and isinstance(last.get("action_coordinate_system"), dict) else None
        reference = action_space or target
        reference_name = "last_screenshot" if action_space else "selected_window"
        if not isinstance(reference, dict):
            image_size = last.get("image_size") if isinstance(last, dict) and isinstance(last.get("image_size"), dict) else {}
            reference = {
                "x": 0,
                "y": 0,
                "width": image_size.get("width", 0),
                "height": image_size.get("height", 0),
            }
            reference_name = "last_screenshot"
        try:
            ref_x = int(reference.get("x", 0))
            ref_y = int(reference.get("y", 0))
            ref_width = int(reference.get("width", 0))
            ref_height = int(reference.get("height", 0))
        except Exception:
            return None, None
        if ref_width <= 0 or ref_height <= 0:
            return None, None
        screen_x = ref_x + round(x * max(ref_width - 1, 0) / 1000)
        screen_y = ref_y + round(y * max(ref_height - 1, 0) / 1000)
        action_payload = dict(payload)
        action_payload["x"] = int(screen_x)
        action_payload["y"] = int(screen_y)
        action_payload["coordinate_space"] = "screen"
        marker = {
            "x": int(round(x)),
            "y": int(round(y)),
            "normalized_x": int(round(x)),
            "normalized_y": int(round(y)),
            "screen_x": int(screen_x),
            "screen_y": int(screen_y),
            "coordinate_space": "normalized_1000",
            "point_order": "yx",
            "reference": reference_name,
        }
        return action_payload, marker

    @staticmethod
    def _coordinate_from_payload(payload: dict[str, Any], keys: tuple[str, ...], default: Any = 0) -> int:
        for key in keys:
            if key in payload and payload.get(key) is not None:
                try:
                    return int(float(payload.get(key)))
                except Exception:
                    continue
        try:
            return int(float(default))
        except Exception:
            return 0

    def _resolve_drag_points(
        self,
        payload: dict[str, Any],
        *,
        remember_cursor: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        state = self._computer_state()
        if not self._state_matches_artifact_root(state):
            state = {}
        cursor = state.get("ai_cursor") if isinstance(state.get("ai_cursor"), dict) else {}
        start_x = self._coordinate_from_payload(payload, ("x1", "from_x", "start_x"), cursor.get("x", 0))
        start_y = self._coordinate_from_payload(payload, ("y1", "from_y", "start_y"), cursor.get("y", 0))
        end_x = self._coordinate_from_payload(payload, ("x2", "to_x", "end_x", "x"), start_x)
        end_y = self._coordinate_from_payload(payload, ("y2", "to_y", "end_y", "y"), start_y)

        start_payload = dict(payload)
        start_payload["x"] = start_x
        start_payload["y"] = start_y
        start_action, start_marker = self._resolve_action_point(start_payload, remember_cursor=remember_cursor)

        end_payload = dict(payload)
        end_payload["x"] = end_x
        end_payload["y"] = end_y
        end_action, end_marker = self._resolve_action_point(end_payload, infer_window=True, remember_cursor=remember_cursor)

        action_payload = dict(end_action)
        action_payload["x1"] = int(start_action.get("x", 0))
        action_payload["y1"] = int(start_action.get("y", 0))
        action_payload["x2"] = int(end_action.get("x", 0))
        action_payload["y2"] = int(end_action.get("y", 0))
        action_payload["x"] = int(end_action.get("x", 0))
        action_payload["y"] = int(end_action.get("y", 0))

        drag_marker = None
        if start_marker or end_marker:
            drag_marker = {
                "from": start_marker or {"x": start_x, "y": start_y},
                "to": end_marker or {"x": end_x, "y": end_y},
            }
        return action_payload, end_marker, drag_marker

    @staticmethod
    def _point_looks_like_model_coordinate(x: int, y: int, state: dict[str, Any]) -> bool:
        last = state.get("last_screenshot") if isinstance(state, dict) else None
        if not isinstance(last, dict):
            return False
        model_size = last.get("model_image_size") if isinstance(last.get("model_image_size"), dict) else {}
        action_space = last.get("action_coordinate_system") if isinstance(last.get("action_coordinate_system"), dict) else {}
        try:
            model_width = int(model_size.get("width", 0))
            model_height = int(model_size.get("height", 0))
            action_width = int(action_space.get("width", 0))
            action_height = int(action_space.get("height", 0))
        except Exception:
            return False
        if model_width <= 0 or model_height <= 0 or action_width <= 0 or action_height <= 0:
            return False
        if model_width >= action_width and model_height >= action_height:
            return False
        return 0 <= x <= model_width and 0 <= y <= model_height

    @staticmethod
    def _resolve_model_point(
        payload: dict[str, Any],
        x: int,
        y: int,
        state: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        last = state.get("last_screenshot") if isinstance(state, dict) else None
        if not isinstance(last, dict):
            return None, None
        model_size = last.get("model_image_size") if isinstance(last.get("model_image_size"), dict) else {}
        action_space = last.get("action_coordinate_system") if isinstance(last.get("action_coordinate_system"), dict) else {}
        try:
            model_width = int(model_size.get("width", 0))
            model_height = int(model_size.get("height", 0))
            action_x = int(action_space.get("x", 0))
            action_y = int(action_space.get("y", 0))
            action_width = int(action_space.get("width", 0))
            action_height = int(action_space.get("height", 0))
        except Exception:
            return None, None
        if model_width <= 0 or model_height <= 0 or action_width <= 0 or action_height <= 0:
            return None, None
        screen_x = action_x + round(x * max(action_width - 1, 0) / max(model_width - 1, 1))
        screen_y = action_y + round(y * max(action_height - 1, 0) / max(model_height - 1, 1))
        action_payload = dict(payload)
        action_payload["x"] = int(screen_x)
        action_payload["y"] = int(screen_y)
        action_payload["coordinate_space"] = "screen"
        marker = {
            "x": x,
            "y": y,
            "screen_x": int(screen_x),
            "screen_y": int(screen_y),
            "coordinate_space": "model_image",
        }
        return action_payload, marker

    def _computer_state(self) -> dict[str, Any]:
        sessions = self._read_sessions()
        state = sessions.get("computer") if isinstance(sessions.get("computer"), dict) else {}
        return dict(state)

    def _write_computer_state(self, state: dict[str, Any]) -> None:
        sessions = self._read_sessions()
        sessions["computer"] = state
        sessions["updated_at"] = self._now_iso()
        self._write_sessions(sessions)

    def _state_matches_artifact_root(self, state: dict[str, Any]) -> bool:
        if not self._custom_artifact_root:
            return True
        last = state.get("last_screenshot") if isinstance(state.get("last_screenshot"), dict) else {}
        for key in ("path", "model_image_path", "unmarked_model_image_path"):
            value = last.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                Path(value).resolve().relative_to(self._artifact_root.resolve())
                return True
            except Exception:
                return False
        return True

    def _clear_target_window(self) -> None:
        state = self._computer_state()
        if "target_window" in state:
            state.pop("target_window", None)
            self._write_computer_state(state)

    def _clear_target_app(self) -> None:
        state = self._computer_state()
        if "target_app" in state:
            state.pop("target_app", None)
            self._write_computer_state(state)

    def _set_ai_cursor(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._computer_state()
        x_value = payload.get("x", payload.get("x2", payload.get("to_x", 0)))
        y_value = payload.get("y", payload.get("y2", payload.get("to_y", 0)))
        cursor = {
            "x": int(x_value),
            "y": int(y_value),
            "origin": "top_left",
            "updated_at": self._now_iso(),
        }
        state["ai_cursor"] = cursor
        self._write_computer_state(state)
        return cursor

    def _publish_virtual_pointer(self, pointer: dict[str, Any], *, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(pointer, dict):
            return None
        if platform.system() != "Darwin":
            return None
        try:
            from ..computer.mac.edge_haze import ComputerUseEdgeHazeManager

            manager = ComputerUseEdgeHazeManager.from_pack_root(Path(__file__).resolve().parents[2])
            haze_payload = self._edge_haze_payload(action, payload)
            result = manager.update_virtual_pointer(
                {**pointer, "phase": action.removeprefix("computer.")},
                action=action,
                payload=haze_payload,
            )
            return result if result.get("started") else None
        except Exception:
            return None

    def _remember_last_screenshot(self, result: dict[str, Any]) -> None:
        state = self._computer_state()
        remembered = {
            "updated_at": self._now_iso(),
            "path": result.get("path"),
            "model_image_path": result.get("model_image_path"),
            "image_size": result.get("image_size"),
            "model_image_size": result.get("model_image_size"),
            "action_coordinate_system": result.get("action_coordinate_system"),
            "target_window": result.get("target_window"),
            "crop_reference": result.get("crop_reference"),
        }
        state["last_screenshot"] = {key: value for key, value in remembered.items() if value not in (None, "")}
        if not isinstance(result.get("crop_reference"), dict):
            state["last_full_screenshot"] = state["last_screenshot"]
        else:
            full_reference = self._full_screenshot_reference_from_crop(result)
            if full_reference:
                state["last_full_screenshot"] = full_reference
        self._write_computer_state(state)

    def _full_screenshot_reference_from_crop(self, result: dict[str, Any]) -> dict[str, Any] | None:
        crop_reference = result.get("crop_reference") if isinstance(result.get("crop_reference"), dict) else {}
        if crop_reference.get("source_is_crop"):
            return None
        source_path = str(crop_reference.get("source_path") or "").strip()
        if not source_path:
            return None
        source_size = crop_reference.get("source_image_size") if isinstance(crop_reference.get("source_image_size"), dict) else {}
        source_action = crop_reference.get("source_action_coordinate_system")
        remembered = {
            "updated_at": self._now_iso(),
            "path": source_path,
            "image_size": source_size,
            "action_coordinate_system": source_action if isinstance(source_action, dict) else None,
            "target_window": result.get("target_window"),
        }
        return {key: value for key, value in remembered.items() if value not in (None, "", {})}

    def _window_at_point(self, x: int, y: int) -> dict[str, Any] | None:
        for item in self._list_windows():
            window = self._normalize_window_record(item)
            if window is None:
                continue
            left = int(window.get("x", 0))
            top = int(window.get("y", 0))
            right = left + int(window.get("width", 0))
            bottom = top + int(window.get("height", 0))
            if left <= x <= right and top <= y <= bottom:
                return window
        return None

    def _select_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = str(payload.get("target") or "").strip().lower()
        filters = self._window_filter(payload)
        app = filters.get("app", "").lower()
        title = filters.get("title", "").lower()
        require_exact_binding = payload.get("require_exact_binding") is True
        focus_requested = payload.get("focus", True) is not False
        inventory_facts: dict[str, Any] = {
            "selection_requested_alias_valid": bool(self._app_alias_tokens(app)),
            "selection_requested_bundle_alias_available": any(
                self._app_alias_tokens(app) & self._app_alias_tokens(key)
                for key in _DARWIN_BROWSER_BUNDLE_ID_ALIASES
            ),
            "selection_activation_policy": "invalid_requested" if focus_requested else "not_requested",
        }
        selected_identity_observation: Any = None
        use_inventory_diagnostics = bool(
            require_exact_binding
            and (
                "_darwin_window_inventory_observation" in self.__dict__
                or (
                    platform.system() == "Darwin"
                    and "_list_windows" not in self.__dict__
                )
            )
        )
        if use_inventory_diagnostics:
            observation = self._darwin_window_inventory_observation(app)
            windows = list(observation.get("windows") or [])
            if isinstance(observation.get("facts"), dict):
                inventory_facts.update(observation["facts"])
            selected_identity_observation = observation.get("_selected_identity_observation")
            if isinstance(selected_identity_observation, dict):
                inventory_facts.update(
                    self._selected_window_identity_facts(selected_identity_observation, None)
                )
        else:
            windows = self._list_windows()
        has_filter = bool(app or title or isinstance(payload.get("window"), dict))
        if require_exact_binding and focus_requested:
            if has_filter:
                self._clear_target_window()
            return {
                "action": "computer.select_window",
                "selected": False,
                "is_error": True,
                "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
                **inventory_facts,
                **self._selection_binding_facts(
                    None, requested_app=app, matched_app=False, matched_window=False,
                    selected=False, exact_binding_required=True,
                    focus_requested=True, focus_attempted=False,
                    failure_stage="activation_policy",
                ),
            }
        normalized_windows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in windows:
            normalized = self._normalize_window_record(item)
            if normalized is not None:
                normalized_windows.append((normalized, item))
        selection_matched_app = any(
            isinstance(item, dict)
            and self._app_name_matches(app, str(item.get("app") or item.get("process") or ""))
            for item in windows
        ) if app else bool(windows)
        inventory_facts["selection_window_owner_alias_matched"] = bool(selection_matched_app)
        selected: Any = None
        if isinstance(payload.get("window"), dict):
            selected = payload.get("window")
        if selected is None and target in {"selected", "selected_window", "window", "app"}:
            selected = self._computer_state().get("target_window")
        if selected is None and (target in {"active", "active_window", "front", "front_window"} or (not target and not has_filter)):
            selected = next((item for item in windows if item.get("active")), None) or self._active_window()
        if selected is None:
            candidates: list[dict[str, Any]] = []
            candidate_sources: dict[int, dict[str, Any]] = {}
            for window, raw_window in normalized_windows:
                if not self._is_usable_target_window(window):
                    continue
                if not self._window_matches_filter(window, app=app, title=title):
                    continue
                candidates.append(window)
                candidate_sources[id(window)] = raw_window
            best_candidate = self._best_window_candidate(candidates, app=app, title=title)
            if best_candidate is not None:
                selected = candidate_sources.get(id(best_candidate), best_candidate)
        normalized_selected = self._normalize_window_record(selected)
        if isinstance(selected_identity_observation, dict):
            inventory_facts.update(
                self._selected_window_identity_facts(
                    selected_identity_observation, normalized_selected
                )
            )
        if app and isinstance(selected, dict):
            selection_matched_app = selection_matched_app or self._app_name_matches(
                app, str(selected.get("app") or selected.get("process") or "")
            )
        inventory_facts["selection_window_owner_alias_matched"] = bool(selection_matched_app)
        selection_matched_window = bool(
            normalized_selected
            and self._is_usable_target_window(normalized_selected)
            and self._window_matches_filter(normalized_selected, app=app, title=title)
        )
        if selected is None:
            if has_filter:
                self._clear_target_window()
            if require_exact_binding:
                error_code = (
                    "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
                    if app and not selection_matched_app
                    else "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND"
                )
                failure_stage = "window_observation" if error_code == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED" else "window_match"
                return {
                    "action": "computer.select_window",
                    "selected": False,
                    "is_error": True,
                    "error_code": error_code,
                    **inventory_facts,
                    **self._selection_binding_facts(
                        None,
                        requested_app=app,
                        matched_app=selection_matched_app,
                        matched_window=False,
                        selected=False,
                        exact_binding_required=True,
                        focus_requested=focus_requested,
                        focus_attempted=False,
                        failure_stage=failure_stage,
                    ),
                }
            return {"action": "computer.select_window", "selected": False, "windows": windows}
        if normalized_selected is None or not self._is_usable_target_window(normalized_selected):
            if has_filter:
                self._clear_target_window()
            if require_exact_binding:
                return {
                    "action": "computer.select_window",
                    "selected": False,
                    "is_error": True,
                    "error_code": "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND",
                    **inventory_facts,
                    **self._selection_binding_facts(
                        selected,
                        requested_app=app,
                        matched_app=selection_matched_app,
                        matched_window=False,
                        selected=False,
                        exact_binding_required=True,
                        focus_requested=focus_requested,
                        focus_attempted=False,
                        failure_stage="window_match",
                    ),
                }
            return {"action": "computer.select_window", "selected": False, "windows": windows}
        if require_exact_binding and not self._exact_window_binding_present(selected, requested_app=app):
            if has_filter:
                self._clear_target_window()
            return {
                "action": "computer.select_window",
                "selected": False,
                "is_error": True,
                "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
                **inventory_facts,
                **self._selection_binding_facts(
                    selected,
                    requested_app=app,
                    matched_app=selection_matched_app,
                    matched_window=selection_matched_window,
                    selected=False,
                    exact_binding_required=True,
                    focus_requested=focus_requested,
                    focus_attempted=False,
                    failure_stage="exact_binding",
                ),
            }
        selected = normalized_selected
        if require_exact_binding and inventory_facts.get("selection_inventory_instrumentation_consistent") is True:
            inventory_facts["selection_inventory_diagnostic_stage"] = "complete"
            inventory_facts["selection_inventory_diagnostic_outcome"] = "exact_window_ready"
            inventory_facts["selection_inventory_cause_count"] = 0
        state = self._computer_state()
        state["target_window"] = selected
        self._write_computer_state(state)
        if focus_requested:
            self._focus_window(selected)
        return {
            "action": "computer.select_window",
            "selected": True,
            "target_window": selected,
            "windows": windows,
            "coordinate_space": "screenshot_image",
            "computer_seat": self._computer_seat_metadata_for_target(selected),
            **inventory_facts,
            **self._selection_binding_facts(
                selected,
                requested_app=app,
                matched_app=selection_matched_app,
                matched_window=True,
                selected=True,
                exact_binding_required=require_exact_binding,
                focus_requested=focus_requested,
                focus_attempted=focus_requested,
                failure_stage="none",
            ),
        }

    @classmethod
    def _selection_binding_facts(
        cls,
        value: Any,
        *,
        requested_app: str,
        matched_app: bool,
        matched_window: bool,
        selected: bool,
        exact_binding_required: bool,
        focus_requested: bool,
        focus_attempted: bool,
        failure_stage: str,
    ) -> dict[str, Any]:
        target = value if isinstance(value, dict) else {}
        geometry_values = [target.get(key) for key in ("x", "y", "width", "height")]
        geometry_complete = all(item is not None for item in geometry_values)
        geometry_integral = geometry_complete and all(
            cls._integral_window_number(item, positive=key in {"width", "height"}) is not None
            for key, item in zip(("x", "y", "width", "height"), geometry_values)
        )
        app_verified = bool(
            requested_app
            and cls._app_name_matches(requested_app, str(target.get("app") or target.get("process") or ""))
        )
        pid_present = cls._integral_window_number(target.get("pid"), positive=True) is not None
        window_id_present = cls._integral_window_number(
            target.get("window_id") if target.get("window_id") is not None else target.get("id"),
            positive=True,
        ) is not None
        exact_present = bool(
            app_verified
            and pid_present
            and window_id_present
            and geometry_complete
            and geometry_integral
        )
        return {
            "selection_matched_app": bool(matched_app),
            "selection_matched_window": bool(matched_window),
            "selection_selected": bool(selected),
            "selection_exact_binding_required": bool(exact_binding_required),
            "selection_exact_binding_present": exact_present,
            "selection_app_verified": app_verified,
            "selection_pid_present": pid_present,
            "selection_window_id_present": window_id_present,
            "selection_geometry_complete": geometry_complete,
            "selection_geometry_integral": geometry_integral,
            "selection_focus_requested": bool(focus_requested),
            "selection_focus_attempted": bool(focus_attempted),
            "selection_failure_stage": failure_stage,
        }

    @classmethod
    def _exact_window_binding_present(cls, value: Any, *, requested_app: str) -> bool:
        facts = cls._selection_binding_facts(
            value,
            requested_app=requested_app,
            matched_app=True,
            matched_window=True,
            selected=True,
            exact_binding_required=True,
            focus_requested=False,
            focus_attempted=False,
            failure_stage="none",
        )
        return facts["selection_exact_binding_present"] is True

    @staticmethod
    def _integral_window_number(value: Any, *, positive: bool) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not number.is_integer() or (positive and number <= 0):
            return None
        return int(number)

    def _list_windows(self) -> list[dict[str, Any]]:
        system = platform.system()
        if system == "Darwin":
            return self._darwin_windows()
        if system == "Windows":
            windows = self._windows_windows()
            if windows:
                return windows
            active = self._windows_active_window()
            return [active] if active else []
        if system == "Linux":
            try:
                from ..computer.linux import xdotool

                return xdotool.list_windows()
            except Exception:
                return []
        return []

    def _active_window(self) -> dict[str, Any] | None:
        system = platform.system()
        if system == "Darwin":
            windows = self._darwin_windows()
            return next((item for item in windows if item.get("active")), None)
        if system == "Windows":
            return self._windows_active_window()
        if system == "Linux":
            return next((item for item in self._list_windows() if item.get("active")), None)
        return None

    def _active_window_for_app(self, app_name: str) -> dict[str, Any] | None:
        app_name = app_name.strip().lower()
        if not app_name:
            return None
        active = self._active_window()
        if active and self._app_name_matches(app_name, str(active.get("app") or "")) and self._is_usable_target_window(active):
            return self._normalize_window_record(active)
        for item in self._list_windows():
            window = self._normalize_window_record(item)
            if (
                window
                and self._is_usable_target_window(window)
                and self._app_name_matches(app_name, str(window.get("app") or ""))
            ):
                return window
        return None

    @staticmethod
    def _normalize_window_record(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        try:
            x = int(float(value.get("x", 0)))
            y = int(float(value.get("y", 0)))
            width = int(float(value.get("width", 0)))
            height = int(float(value.get("height", 0)))
        except Exception:
            return None
        if width <= 0 or height <= 0:
            return None
        normalized = {
            "app": str(value.get("app") or value.get("process") or ""),
            "title": str(value.get("title") or value.get("name") or ""),
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "active": bool(value.get("active")),
        }
        try:
            pid = int(value.get("pid") or 0)
        except Exception:
            pid = 0
        if pid > 0:
            normalized["pid"] = pid
        window_id = value.get("window_id") or value.get("id")
        parsed_window_id = BrowserComputerController._window_id_int(window_id)
        if parsed_window_id:
            normalized["window_id"] = parsed_window_id
        hwnd = BrowserComputerController._window_id_int(value.get("hwnd"))
        if hwnd:
            normalized["hwnd"] = hwnd
        for rect_key in ("capture_rect", "content_rect"):
            rect = BrowserComputerController._normalize_rect(value.get(rect_key))
            if rect:
                normalized[rect_key] = rect
        frame_ids = value.get("frame_window_ids")
        if isinstance(frame_ids, list):
            ids: list[int] = []
            for item in frame_ids:
                try:
                    ids.append(int(item))
                except Exception:
                    continue
            if ids:
                normalized["frame_window_ids"] = ids
        capture_method = str(value.get("capture_method") or "").strip()
        if capture_method:
            normalized["capture_method"] = capture_method
        return normalized

    @staticmethod
    def _window_id_int(value: Any) -> int:
        try:
            text = str(value or "").strip()
            return int(text, 0) if text else 0
        except Exception:
            return 0

    @staticmethod
    def _is_usable_target_window(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            width = int(float(value.get("width", 0)))
            height = int(float(value.get("height", 0)))
        except Exception:
            return False
        return width >= 200 and height >= 120

    @classmethod
    def _best_window_candidate(
        cls,
        candidates: list[dict[str, Any]],
        *,
        app: str = "",
        title: str = "",
    ) -> dict[str, Any] | None:
        if not candidates:
            return None

        def area_of_rect(value: Any) -> int:
            rect = cls._normalize_rect(value)
            if not rect:
                return 0
            return int(rect.get("width", 0)) * int(rect.get("height", 0))

        def score(index_and_window: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int, int, int]:
            index, window = index_and_window
            width = int(window.get("width", 0))
            height = int(window.get("height", 0))
            area = width * height
            capture_area = area_of_rect(window.get("capture_rect")) or area
            content_area = area_of_rect(window.get("content_rect")) or area
            title_text = str(window.get("title") or "").lower()
            title_bonus = 0
            if title:
                title_bonus = 2 if title_text == title else 1
            active_bonus = 1 if window.get("active") else 0
            # Quartz often reports Chrome extension popovers before the real page window.
            # When the caller only specifies an app, the full page/window is the safer target.
            return (title_bonus, capture_area, content_area, area, active_bonus, -index)

        return max(enumerate(candidates), key=score)[1]

    @staticmethod
    def _normalize_rect(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        try:
            x = int(round(float(value.get("x", value.get("X", 0)) or 0)))
            y = int(round(float(value.get("y", value.get("Y", 0)) or 0)))
            width = int(round(float(value.get("width", value.get("Width", 0)) or 0)))
            height = int(round(float(value.get("height", value.get("Height", 0)) or 0)))
        except Exception:
            return None
        if width <= 0 or height <= 0:
            return None
        return {"x": x, "y": y, "width": width, "height": height}

    @classmethod
    def _selection_source_facts(
        cls,
        source: str,
        windows: list[dict[str, Any]],
        *,
        app: str,
        observed: bool,
        contract_valid: bool,
        total_count: int | None = None,
        target_pids: set[int] | None = None,
        bundle_pids: set[int] | None = None,
        pid_match_available: bool = False,
        bundle_match_available: bool = False,
        on_screen_only: bool,
        layer_zero: bool,
    ) -> dict[str, Any]:
        usable = [item for item in windows if cls._is_usable_target_window(item)]
        target_pids = target_pids or set()
        bundle_pids = bundle_pids or set()
        name_matches = sum(
            1 for item in usable
            if cls._app_name_matches(app, str(item.get("app") or item.get("process") or ""))
        )
        pid_matches = sum(
            1 for item in usable
            if cls._integral_window_number(item.get("pid"), positive=True) in target_pids
        ) if pid_match_available else 0
        bundle_matches = sum(
            1 for item in usable
            if cls._integral_window_number(item.get("pid"), positive=True) in bundle_pids
        ) if bundle_match_available else 0
        prefix = f"selection_{source}_"
        return {
            f"{prefix}inventory_observed": bool(observed),
            f"{prefix}inventory_contract_valid": bool(contract_valid),
            f"{prefix}window_total_count": min(64, max(0, total_count if total_count is not None else len(windows))),
            f"{prefix}usable_window_count": min(64, len(usable)),
            f"{prefix}target_name_match_count": min(8, name_matches),
            f"{prefix}target_pid_match_count": min(8, pid_matches),
            f"{prefix}target_bundle_match_count": min(8, bundle_matches),
            f"{prefix}pid_match_available": bool(pid_match_available),
            f"{prefix}bundle_match_available": bool(bundle_match_available),
            f"{prefix}on_screen_only_filter_applied": bool(on_screen_only),
            f"{prefix}layer_zero_filter_applied": bool(layer_zero),
        }

    @classmethod
    def _selected_window_identity_inventory(
        cls, result: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Consume helper-local identity flags without retaining them in records.

        This is an inventory diagnostic, never an ownership or selection
        predicate. The private mapping is used only for an exact selected
        (pid, window_id) correlation and never enters state or public output.
        """
        marker_valid = (
            isinstance(result, dict)
            and result.get("selected_window_identity_diagnostic_contract")
            == _SELECTED_WINDOW_IDENTITY_DIAGNOSTIC_CONTRACT
        )
        raw_windows = result.get("windows") if isinstance(result, dict) else None
        contract_valid = marker_valid and isinstance(raw_windows, list)
        records: dict[tuple[int, int], tuple[bool, bool, bool]] = {}
        public_windows: list[dict[str, Any]] = []
        if not isinstance(raw_windows, list):
            return public_windows, {"contract_valid": False, "records": records}
        for item in raw_windows:
            if not isinstance(item, dict):
                contract_valid = False
                continue
            # Drop every helper-private annotation before a record can be
            # selected, persisted, returned, or handed to another source.
            public = {
                key: value for key, value in item.items()
                if not str(key).startswith("_rumi_")
            }
            public_windows.append(public)
            pid = cls._integral_window_number(item.get("pid"), positive=True)
            window_id = cls._integral_window_number(
                item.get("window_id") if item.get("window_id") is not None else item.get("id"),
                positive=True,
            )
            flags = tuple(item.get(key) for key in _SELECTED_WINDOW_IDENTITY_PRIVATE_FIELDS)
            if (
                pid is None
                or window_id is None
                or len(flags) != 3
                or any(not isinstance(value, bool) for value in flags)
            ):
                contract_valid = False
                continue
            owner_alias_match, target_process_match, target_bundle_match = flags
            if target_bundle_match and not target_process_match:
                contract_valid = False
                continue
            binding = (pid, window_id)
            if binding in records:
                contract_valid = False
                continue
            records[binding] = (
                owner_alias_match, target_process_match, target_bundle_match,
            )
        return public_windows, {"contract_valid": contract_valid, "records": records}

    @classmethod
    def _selected_window_identity_facts(
        cls, observation: Any, selected: Any,
    ) -> dict[str, Any]:
        """Produce only closed selected-record facts without changing selection."""
        contract_valid = bool(
            isinstance(observation, dict) and observation.get("contract_valid") is True
        )
        facts: dict[str, Any] = {
            "selection_selected_identity_contract_valid": contract_valid,
            "selection_selected_identity_available": False,
            "selection_selected_owner_alias_match": False,
            "selection_selected_target_process_match": False,
            "selection_selected_target_bundle_match": False,
            "selection_selected_identity_class": "unavailable",
        }
        if not contract_valid or not isinstance(selected, dict):
            return facts
        pid = cls._integral_window_number(selected.get("pid"), positive=True)
        window_id = cls._integral_window_number(
            selected.get("window_id") if selected.get("window_id") is not None else selected.get("id"),
            positive=True,
        )
        records = observation.get("records") if isinstance(observation.get("records"), dict) else {}
        values = records.get((pid, window_id)) if pid is not None and window_id is not None else None
        if (
            not isinstance(values, tuple)
            or len(values) != 3
            or any(not isinstance(value, bool) for value in values)
        ):
            return facts
        owner_alias_match, target_process_match, target_bundle_match = values
        # A bundle assertion without the target process violates the native
        # contract. Fail closed without altering the selection result.
        if target_bundle_match and not target_process_match:
            return facts
        facts.update({
            "selection_selected_identity_available": True,
            "selection_selected_owner_alias_match": owner_alias_match,
            "selection_selected_target_process_match": target_process_match,
            "selection_selected_target_bundle_match": target_bundle_match,
            "selection_selected_identity_class": (
                "bundle_process_match" if target_bundle_match
                else "process_match" if target_process_match
                else "owner_name_only" if owner_alias_match
                else "no_match"
            ),
        })
        return facts

    def _darwin_swift_inventory_observation(
        self, *, app: str, aliases: set[str], bundle_aliases: set[str]
    ) -> dict[str, Any]:
        helper_defaults = {
            "selection_swift_helper_available": False,
            "selection_swift_helper_invoked": False,
            "selection_swift_helper_response_contract": "not_invoked",
            "selection_swift_helper_binary_class": "unavailable",
            "selection_swift_helper_contract_version_class": "missing",
            "selection_swift_helper_compile_attempted": False,
            "selection_swift_helper_compile_succeeded": False,
            "selection_swift_helper_persistence_class": "unavailable",
            "selection_swift_helper_path_stability": "unavailable",
            "selection_swift_helper_signature_stability": "unavailable",
        }
        result: dict[str, Any] = {}
        helper_facts = dict(helper_defaults)
        try:
            from ..computer.mac.swift_host import MacSwiftComputerHost

            host = MacSwiftComputerHost()
            result, observed_facts = host.run_with_facts(
                "computer.windows",
                {
                    "inventory_diagnostics": True,
                    "target_aliases": sorted(aliases),
                    "target_bundle_aliases": sorted(bundle_aliases),
                },
            )
            if isinstance(observed_facts, dict):
                helper_facts.update(observed_facts)
        except Exception:
            helper_facts["selection_swift_helper_response_contract"] = "process_failure"
        windows, selected_identity_observation = self._selected_window_identity_inventory(result)
        native_facts = result.get("inventory_diagnostics") if isinstance(result.get("inventory_diagnostics"), dict) else {}
        # The v3 topology probe deliberately keeps process identities inside
        # the native helper.  Selection still consumes the unchanged on-screen
        # window records; these empty sets only make secondary diagnostics
        # conservative rather than turning an off-screen correlation into an
        # actionable ownership claim.
        target_pids: set[int] = set()
        bundle_pids: set[int] = set()
        contract_valid = bool(
            helper_facts.get("selection_swift_helper_response_contract") == "valid_success"
            and helper_facts.get("selection_swift_helper_contract_version_class") == "expected"
            and native_facts.get("selection_swift_inventory_contract_valid") is True
        )
        facts = self._selection_source_facts(
            "swift", windows, app=app,
            observed=helper_facts.get("selection_swift_helper_invoked") is True,
            contract_valid=contract_valid,
            total_count=self._integral_window_number(native_facts.get("selection_swift_window_total_count"), positive=False),
            target_pids=target_pids, bundle_pids=bundle_pids,
            pid_match_available=native_facts.get("selection_swift_pid_match_available") is True,
            bundle_match_available=native_facts.get("selection_swift_bundle_match_available") is True,
            on_screen_only=True, layer_zero=True,
        )
        for key in (
            "selection_native_snapshot_atomic", "selection_nsworkspace_observation_completed",
            "selection_nsworkspace_target_process_present", "selection_nsworkspace_localized_name_match",
            "selection_nsworkspace_bundle_id_match", "selection_target_pid_match_available",
            "selection_target_bundle_match_available",
        ):
            facts[key] = native_facts.get(key) is True
        facts["selection_nsworkspace_target_process_match_count"] = min(
            4, self._integral_window_number(
                native_facts.get("selection_nsworkspace_target_process_match_count"), positive=False
            ) or 0,
        )
        native_bool_fields = (
            "selection_swift_permission_check_colocated",
            "selection_permission_request_api_invoked",
            "selection_swift_target_pid_set_constructed_privately",
            "selection_swift_on_screen_omission_confirmed",
            "selection_swift_all_windows_nonactionable",
            "selection_swift_visibility_probe_performed",
            "selection_swift_visibility_probe_complete",
            "selection_swift_visibility_probe_truncated",
            "selection_swift_target_hidden_present",
            "selection_swift_target_unhidden_present",
            "selection_swift_target_ax_windows_read_complete",
        )
        for key in native_bool_fields:
            if isinstance(native_facts.get(key), bool):
                facts[key] = native_facts[key]
        native_enum_domains = {
            "selection_swift_execution_component": {"viewer_app", "isolated_python_runtime", "swift_helper", "system_events_child", "other", "unknown"},
            "selection_swift_helper_signing_class": {"signed_stable", "ad_hoc", "unsigned", "unavailable", "unknown"},
            "selection_codex_permission_comparison": {"not_observable"},
            "selection_swift_ax_trust": {"trusted", "not_trusted", "unavailable"},
            "selection_swift_ax_target_probe_outcome": {
                "success", "skipped_not_trusted", "api_disabled", "invalid_ui_element",
                "cannot_complete", "attribute_unsupported", "no_value", "illegal_argument",
                "failure", "unavailable", "unknown",
            },
            "selection_swift_screen_capture_preflight": {"granted", "denied", "unavailable"},
            "selection_swift_cg_on_screen_query_outcome": {"success_nonempty", "success_nonempty_truncated", "success_empty", "nil_or_unavailable", "invalid_payload"},
            "selection_swift_cg_all_windows_query_outcome": {"success_nonempty", "success_nonempty_truncated", "success_empty", "nil_or_unavailable", "invalid_payload"},
            "selection_swift_visibility_class": {
                "on_screen_nonfrontmost", "on_screen_frontmost", "app_hidden",
                "all_ax_windows_minimized", "offscreen_same_pid_frame_correlated",
                "offscreen_cross_pid_frame_correlated", "off_display_geometry",
                "multiple_process_ambiguous", "ax_windows_unavailable", "mixed",
                "indeterminate",
            },
            "selection_swift_visibility_incomplete_cause": {
                "none", "target_process_cap", "ax_window_cap", "cg_record_cap",
                "ax_read_failure", "protocol_invalid", "multiple_candidates",
            },
        }
        for key, allowed in native_enum_domains.items():
            if native_facts.get(key) in allowed:
                facts[key] = native_facts[key]
        native_count_caps = {
            "selection_swift_owner_name_present_count": 64,
            "selection_swift_window_name_present_count": 64,
            "selection_swift_raw_target_pid_match_count": 8,
            "selection_swift_raw_target_bundle_match_count": 8,
            "selection_swift_all_windows_target_pid_match_count": 8,
            "selection_swift_target_rejected_not_on_screen_count": 8,
            "selection_swift_target_rejected_nonzero_layer_count": 8,
            "selection_swift_target_rejected_invalid_identity_count": 8,
            "selection_swift_target_rejected_nonpositive_geometry_count": 8,
            "selection_swift_rejected_target_pid_mismatch_count": 64,
            "selection_swift_rejected_target_bundle_mismatch_count": 8,
            "selection_swift_visibility_target_process_count": 4,
            "selection_swift_visibility_candidate_process_count": 4,
            "selection_swift_target_ax_window_count": 16,
            "selection_swift_ax_minimized_count": 16,
            "selection_swift_ax_nonminimized_count": 16,
            "selection_swift_ax_frame_valid_count": 16,
            "selection_swift_ax_display_intersection_count": 16,
            "selection_swift_ax_same_pid_cg_frame_match_count": 16,
            "selection_swift_ax_cross_pid_cg_frame_match_count": 16,
            "selection_swift_target_cg_offscreen_layer_zero_geometry_count": 16,
        }
        for key, cap in native_count_caps.items():
            value = native_facts.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                facts[key] = max(0, min(cap, value))
        return {
            "windows": windows,
            "facts": {**helper_facts, **facts},
            "target_pids": target_pids,
            "bundle_pids": bundle_pids,
            # This is controller-local and consumed before _select_window
            # returns. It contains no public window record annotations.
            "_selected_identity_observation": selected_identity_observation,
        }

    def _darwin_quartz_permission_observation(
        self, *, app: str, aliases: set[str], target_pids: set[int], bundle_pids: set[int]
    ) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "selection_quartz_execution_component": "isolated_python_runtime",
            "selection_quartz_permission_check_colocated": True,
            "selection_permission_request_api_invoked": False,
            "selection_quartz_ax_trust": "unavailable",
            "selection_quartz_ax_target_probe_outcome": "unavailable",
            "selection_quartz_screen_capture_preflight": "unavailable",
            "selection_quartz_cg_on_screen_query_outcome": "nil_or_unavailable",
            "selection_quartz_cg_all_windows_query_outcome": "nil_or_unavailable",
            "selection_quartz_cg_all_windows_records_aggregated_count": 0,
            "selection_quartz_owner_name_present_count": 0,
            "selection_quartz_window_name_present_count": 0,
            "selection_quartz_target_pid_set_constructed_privately": bool(target_pids),
            "selection_quartz_raw_target_pid_match_count": 0,
            "selection_quartz_raw_target_bundle_match_count": 0,
            "selection_quartz_all_windows_target_pid_match_count": 0,
            "selection_quartz_target_rejected_not_on_screen_count": 0,
            "selection_quartz_target_rejected_nonzero_layer_count": 0,
            "selection_quartz_target_rejected_invalid_identity_count": 0,
            "selection_quartz_target_rejected_nonpositive_geometry_count": 0,
            "selection_quartz_rejected_target_pid_mismatch_count": 0,
            "selection_quartz_rejected_target_bundle_mismatch_count": 0,
            "selection_quartz_on_screen_omission_confirmed": False,
            "selection_quartz_all_windows_nonactionable": True,
        }
        prelude = "\n".join([
            f"TARGET_ALIASES = set({json.dumps(sorted(aliases))})",
            f"TARGET_PIDS = set({json.dumps(sorted(target_pids))})",
            f"BUNDLE_PIDS = set({json.dumps(sorted(bundle_pids))})",
            f"MAX_BRIDGED_RECORDS = {_QUARTZ_BRIDGE_MAX_ITEMS}",
        ])
        code = prelude + r'''
import json
import re
import Quartz

def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

TARGET_ALIASES = {norm(value) for value in TARGET_ALIASES if norm(value)}

def has_mapping_capability(value):
    return any(callable(getattr(value, name, None)) for name in (
        "get", "objectForKey_", "__getitem__",
    ))

def mapping_get(value, key, default=None):
    getter = getattr(value, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                result = getter(key)
                return default if result is None else result
            except Exception:
                pass
        except Exception:
            pass
    object_for_key = getattr(value, "objectForKey_", None)
    if callable(object_for_key):
        try:
            result = object_for_key(key)
            return default if result is None else result
        except Exception:
            pass
    get_item = getattr(value, "__getitem__", None)
    if callable(get_item):
        try:
            return get_item(key)
        except Exception:
            pass
    return default

def bounded_iterable(value):
    if isinstance(value, (str, bytes, bytearray)):
        return None, False

    def validated(records, truncated, canary=None):
        inspected = records if canary is None else [*records, canary]
        if any(not has_mapping_capability(item) for item in inspected):
            return None, False
        return records, truncated

    def as_size(value):
        if isinstance(value, bool):
            return None
        try:
            size = int(value)
        except Exception:
            return None
        return size if size >= 0 else None

    def bounded_indexed(size, item_at_index):
        try:
            records = [item_at_index(index) for index in range(min(size, MAX_BRIDGED_RECORDS))]
        except Exception:
            return None, False
        return validated(records, size > MAX_BRIDGED_RECORDS)

    # PyObjC collections commonly expose a reliable Python length, even when
    # they are not normal Python lists.  Use it to bound the aggregation
    # without probing beyond the public collection size.
    try:
        length = as_size(len(value))
    except Exception:
        length = None
    get_item = getattr(value, "__getitem__", None)
    if length is not None and callable(get_item):
        return bounded_indexed(length, get_item)

    # NSArray-style bridges may omit Python iteration/len but provide the
    # Objective-C count/objectAtIndex_ pair.  This has the same bounded,
    # aggregate-only contract as the Python sequence path.
    count = getattr(value, "count", None)
    item_at_index = getattr(value, "objectAtIndex_", None)
    if callable(count) and callable(item_at_index):
        try:
            length = as_size(count())
        except Exception:
            length = None
        if length is None:
            return None, False
        return bounded_indexed(length, item_at_index)

    # Generic iterables have no reliable size.  Aggregate at most the cap and
    # inspect exactly one additional canary.  A malformed inspected record or
    # canary is invalid_payload; a valid canary means valid-but-truncated.
    try:
        iterator = iter(value)
    except Exception:
        return None, False
    records = []
    if length is not None:
        try:
            for _ in range(min(length, MAX_BRIDGED_RECORDS)):
                records.append(next(iterator))
        except (StopIteration, Exception):
            return None, False
        return validated(records, length > MAX_BRIDGED_RECORDS)
    for _ in range(MAX_BRIDGED_RECORDS):
        try:
            records.append(next(iterator))
        except StopIteration:
            return validated(records, False)
        except Exception:
            return None, False
    try:
        canary = next(iterator)
    except StopIteration:
        return validated(records, False)
    except Exception:
        return None, False
    return validated(records, True, canary)

def number(value):
    try:
        return int(value or 0)
    except Exception:
        return 0

def decimal(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0

def query(options):
    try:
        value = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
    except Exception:
        return [], "nil_or_unavailable"
    if value is None:
        return [], "nil_or_unavailable"
    records, truncated = bounded_iterable(value)
    if records is None:
        return [], "invalid_payload"
    if not records:
        return records, "success_empty"
    return records, "success_nonempty_truncated" if truncated else "success_nonempty"

facts = {
    "selection_quartz_execution_component": "isolated_python_runtime",
    "selection_quartz_permission_check_colocated": True,
    "selection_permission_request_api_invoked": False,
    "selection_quartz_all_windows_nonactionable": True,
    "selection_quartz_target_pid_set_constructed_privately": bool(TARGET_PIDS),
}
try:
    facts["selection_quartz_screen_capture_preflight"] = (
        "granted" if Quartz.CGPreflightScreenCaptureAccess() else "denied"
    )
except Exception:
    facts["selection_quartz_screen_capture_preflight"] = "unavailable"

try:
    from ApplicationServices import (
        AXIsProcessTrusted, AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
    )
    trusted = bool(AXIsProcessTrusted())
    facts["selection_quartz_ax_trust"] = "trusted" if trusted else "not_trusted"
    if not trusted:
        facts["selection_quartz_ax_target_probe_outcome"] = "skipped_not_trusted"
    elif not TARGET_PIDS:
        facts["selection_quartz_ax_target_probe_outcome"] = "unavailable"
    else:
        result = AXUIElementCopyAttributeValue(
            AXUIElementCreateApplication(sorted(TARGET_PIDS)[0]), "AXRole", None
        )
        error = int(result[0]) if isinstance(result, tuple) and result else 0
        mapping = {
            0: "success", -25201: "illegal_argument", -25202: "invalid_ui_element",
            -25203: "invalid_ui_element", -25204: "cannot_complete",
            -25205: "attribute_unsupported", -25211: "api_disabled", -25212: "no_value",
        }
        facts["selection_quartz_ax_target_probe_outcome"] = mapping.get(error, "failure")
except Exception:
    facts["selection_quartz_ax_trust"] = "unavailable"
    facts["selection_quartz_ax_target_probe_outcome"] = "unavailable"

on_records, on_outcome = query(
    Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
)
all_records, all_outcome = query(
    Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements
)
facts["selection_quartz_cg_on_screen_query_outcome"] = on_outcome
facts["selection_quartz_cg_all_windows_query_outcome"] = all_outcome
facts["selection_quartz_cg_all_windows_records_aggregated_count"] = len(all_records)
facts["selection_quartz_owner_name_present_count"] = sum(
    1 for item in on_records if str(mapping_get(item, "kCGWindowOwnerName") or "")
)
facts["selection_quartz_window_name_present_count"] = sum(
    1 for item in on_records if str(mapping_get(item, "kCGWindowName") or "")
)
counts = {
    "raw_pid": 0, "raw_bundle": 0, "all_pid": 0, "not_screen": 0,
    "layer": 0, "identity": 0, "geometry": 0, "pid_mismatch": 0,
    "bundle_mismatch": 0,
}
for item in on_records:
    pid = number(mapping_get(item, "kCGWindowOwnerPID"))
    wid = number(mapping_get(item, "kCGWindowNumber"))
    owner_match = norm(mapping_get(item, "kCGWindowOwnerName")) in TARGET_ALIASES
    pid_match = pid in TARGET_PIDS
    target = owner_match or pid_match
    if pid_match: counts["raw_pid"] += 1
    if pid in BUNDLE_PIDS: counts["raw_bundle"] += 1
    if pid > 0 and wid > 0 and pid not in TARGET_PIDS: counts["pid_mismatch"] += 1
    if owner_match and BUNDLE_PIDS and pid not in BUNDLE_PIDS: counts["bundle_mismatch"] += 1
    if target and (pid <= 0 or wid <= 0): counts["identity"] += 1
    if target and number(mapping_get(item, "kCGWindowLayer")) != 0: counts["layer"] += 1
    bounds = mapping_get(item, "kCGWindowBounds")
    if target and (
        not has_mapping_capability(bounds)
        or decimal(mapping_get(bounds, "Width")) <= 0
        or decimal(mapping_get(bounds, "Height")) <= 0
    ): counts["geometry"] += 1
for item in all_records:
    pid = number(mapping_get(item, "kCGWindowOwnerPID"))
    if pid in TARGET_PIDS:
        counts["all_pid"] += 1
        if not bool(mapping_get(item, "kCGWindowIsOnscreen", False)): counts["not_screen"] += 1

field_map = {
    "raw_pid": "selection_quartz_raw_target_pid_match_count",
    "raw_bundle": "selection_quartz_raw_target_bundle_match_count",
    "all_pid": "selection_quartz_all_windows_target_pid_match_count",
    "not_screen": "selection_quartz_target_rejected_not_on_screen_count",
    "layer": "selection_quartz_target_rejected_nonzero_layer_count",
    "identity": "selection_quartz_target_rejected_invalid_identity_count",
    "geometry": "selection_quartz_target_rejected_nonpositive_geometry_count",
    "pid_mismatch": "selection_quartz_rejected_target_pid_mismatch_count",
    "bundle_mismatch": "selection_quartz_rejected_target_bundle_mismatch_count",
}
for key, field in field_map.items(): facts[field] = counts[key]
facts["selection_quartz_on_screen_omission_confirmed"] = (
    counts["raw_pid"] == 0 and counts["all_pid"] > 0
)
print(json.dumps(facts))
'''
        try:
            completed = subprocess.run(
                _current_python_snippet_command(code), check=True,
                capture_output=True, text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
            observed = json.loads(completed.stdout or "{}")
        except Exception:
            return defaults
        if not isinstance(observed, dict):
            return defaults
        safe = dict(defaults)
        for key, default in defaults.items():
            value = observed.get(key)
            if isinstance(default, bool) and isinstance(value, bool):
                safe[key] = value
            elif isinstance(default, int) and not isinstance(default, bool) and isinstance(value, int) and not isinstance(value, bool):
                cap = (
                    _QUARTZ_BRIDGE_MAX_ITEMS if key.endswith("records_aggregated_count")
                    else 64 if "present_count" in key or "pid_mismatch_count" in key
                    else 8
                )
                safe[key] = min(cap, max(0, value))
            elif isinstance(default, str) and isinstance(value, str):
                if (
                    key.endswith("_query_outcome")
                    and value not in {
                        "success_empty", "success_nonempty", "success_nonempty_truncated",
                        "nil_or_unavailable", "invalid_payload",
                    }
                ):
                    continue
                safe[key] = value
        return safe

    @staticmethod
    def _darwin_system_events_permission_defaults() -> dict[str, Any]:
        return {
            "selection_system_events_execution_component": "system_events_child",
            "selection_system_events_permission_check_colocated": True,
            "selection_permission_request_api_invoked": False,
            "selection_system_events_automation_preflight": "unknown",
            "selection_system_events_execution_outcome": "unknown",
        }

    def _darwin_system_events_automation_preflight(self) -> dict[str, Any]:
        """Check Automation consent without executing AppleScript or prompting."""
        defaults = self._darwin_system_events_permission_defaults()
        code = r'''
import ctypes
import json

facts = {
    "selection_system_events_execution_component": "system_events_child",
    "selection_system_events_permission_check_colocated": True,
    "selection_permission_request_api_invoked": False,
    "selection_system_events_automation_preflight": "unknown",
    "selection_system_events_execution_outcome": "unknown",
}
try:
    framework = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    class AEDesc(ctypes.Structure):
        _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]
    create = framework.AECreateDesc
    create.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_ssize_t, ctypes.POINTER(AEDesc)]
    create.restype = ctypes.c_int32
    determine = framework.AEDeterminePermissionToAutomateTarget
    determine.argtypes = [ctypes.POINTER(AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
    determine.restype = ctypes.c_int32
    dispose = framework.AEDisposeDesc
    target = b"com.apple.systemevents"
    desc = AEDesc()
    status = create(0x62756E64, target, len(target), ctypes.byref(desc))
    if status != 0:
        facts["selection_system_events_automation_preflight"] = "target_unavailable"
        facts["selection_system_events_execution_outcome"] = "not_authorized"
    else:
        permission = determine(ctypes.byref(desc), 0x2A2A2A2A, 0x2A2A2A2A, False)
        dispose(ctypes.byref(desc))
        if permission == 0:
            facts["selection_system_events_automation_preflight"] = "authorized"
        elif permission == -1744:
            facts["selection_system_events_automation_preflight"] = "would_require_consent"
            facts["selection_system_events_execution_outcome"] = "automation_denied"
        elif permission == -1743:
            facts["selection_system_events_automation_preflight"] = "denied"
            facts["selection_system_events_execution_outcome"] = "automation_denied"
        else:
            facts["selection_system_events_automation_preflight"] = "target_unavailable"
            facts["selection_system_events_execution_outcome"] = "not_authorized"
except Exception:
    facts["selection_system_events_automation_preflight"] = "api_unavailable"
    facts["selection_system_events_execution_outcome"] = "launch_failure"
print(json.dumps(facts))
'''
        try:
            completed = subprocess.run(
                _current_python_snippet_command(code), check=True,
                capture_output=True, text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
            observed = json.loads(completed.stdout or "{}")
        except subprocess.TimeoutExpired:
            return {**defaults, "selection_system_events_execution_outcome": "timeout"}
        except FileNotFoundError:
            return {**defaults, "selection_system_events_execution_outcome": "launch_failure"}
        except Exception:
            return {**defaults, "selection_system_events_execution_outcome": "script_failure"}
        if not isinstance(observed, dict):
            return {**defaults, "selection_system_events_execution_outcome": "invalid_output"}
        facts = dict(defaults)
        for key, default in defaults.items():
            value = observed.get(key)
            if isinstance(value, type(default)):
                facts[key] = value
        return facts

    def _darwin_system_events_enumeration(self) -> dict[str, Any]:
        """Enumerate only after the nonprompting preflight says it is authorized."""
        script = r'''
tell application "System Events"
  set output to ""
  repeat with proc in (application processes whose background only is false)
    set procName to name of proc
    set procPid to unix id of proc
    set procFront to frontmost of proc
    repeat with win in windows of proc
      try
        set winName to name of win
        set winPos to position of win
        set winSize to size of win
        set output to output & procName & tab & winName & tab & (item 1 of winPos) & tab & (item 2 of winPos) & tab & (item 1 of winSize) & tab & (item 2 of winSize) & tab & procFront & tab & procPid & linefeed
      end try
    end repeat
  end repeat
  return output
end tell
'''
        code = "SCRIPT = " + json.dumps(script) + r'''
import json

facts = {
    "selection_system_events_execution_outcome": "unknown",
}
output = ""
try:
    from AppKit import NSAppleScript
    result, error = NSAppleScript.alloc().initWithSource_(SCRIPT).executeAndReturnError_(None)
    if error:
        number = int(error.get("NSAppleScriptErrorNumber", 0) or 0)
        message = str(error.get("NSAppleScriptErrorMessage", "") or "").lower()
        if number == -1743:
            facts["selection_system_events_execution_outcome"] = "automation_denied"
        elif "assistive" in message or "accessibility" in message or number == -25211:
            facts["selection_system_events_execution_outcome"] = "accessibility_denied"
        else:
            facts["selection_system_events_execution_outcome"] = "script_failure"
    elif result is None:
        facts["selection_system_events_execution_outcome"] = "invalid_output"
    else:
        output = str(result.stringValue() or "")
        facts["selection_system_events_execution_outcome"] = "success"
except ImportError:
    facts["selection_system_events_execution_outcome"] = "launch_failure"
except Exception:
    facts["selection_system_events_execution_outcome"] = "script_failure"
print(json.dumps({"facts": facts, "output": output}))
'''
        try:
            completed = subprocess.run(
                _current_python_snippet_command(code), check=True,
                capture_output=True, text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
            decoded = json.loads(completed.stdout or "{}")
        except subprocess.TimeoutExpired:
            return {"output": "", "execution_outcome": "timeout"}
        except FileNotFoundError:
            return {"output": "", "execution_outcome": "launch_failure"}
        except Exception:
            return {"output": "", "execution_outcome": "script_failure"}
        if not isinstance(decoded, dict) or not isinstance(decoded.get("facts"), dict):
            return {"output": "", "execution_outcome": "invalid_output"}
        outcome = decoded["facts"].get("selection_system_events_execution_outcome")
        if not isinstance(outcome, str):
            outcome = "invalid_output"
        output = decoded.get("output")
        return {
            "output": output if isinstance(output, str) else "",
            "execution_outcome": outcome if isinstance(output, str) else "invalid_output",
        }

    def _darwin_system_events_permission_observation(
        self, *, app: str, enumerate_windows: bool = True
    ) -> dict[str, Any]:
        """Keep System Events diagnostic-only unless it is the fallback source."""
        del app  # The query is intentionally process-wide, matching the legacy fallback.
        facts = self._darwin_system_events_automation_preflight()
        if not enumerate_windows:
            facts["selection_system_events_execution_outcome"] = "skipped_non_authoritative"
            return {"windows": [], "facts": facts}
        if facts.get("selection_system_events_automation_preflight") != "authorized":
            return {"windows": [], "facts": facts}
        enumeration = self._darwin_system_events_enumeration()
        facts["selection_system_events_execution_outcome"] = str(
            enumeration.get("execution_outcome") or "invalid_output"
        )
        if facts["selection_system_events_execution_outcome"] != "success":
            return {"windows": [], "facts": facts}
        output = enumeration.get("output")
        if not isinstance(output, str):
            facts["selection_system_events_execution_outcome"] = "invalid_output"
            return {"windows": [], "facts": facts}
        windows: list[dict[str, Any]] = []
        invalid = False
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 8:
                invalid = True
                continue
            try:
                window = {
                    "app": parts[0], "title": parts[1],
                    "x": int(float(parts[2])), "y": int(float(parts[3])),
                    "width": int(float(parts[4])), "height": int(float(parts[5])),
                    "active": parts[6].strip().lower() == "true", "pid": int(parts[7]),
                }
            except Exception:
                invalid = True
                continue
            if window["width"] > 0 and window["height"] > 0:
                windows.append(window)
        if invalid and not windows:
            facts["selection_system_events_execution_outcome"] = "invalid_output"
        return {"windows": windows, "facts": facts}

    @staticmethod
    def _selection_permission_outcome(facts: dict[str, Any]) -> str:
        """Global cross-source diagnostic only; it never controls source selection."""
        if facts.get("selection_permission_request_api_invoked") is True:
            return "forbidden_action_required"
        query_values = [
            facts.get(f"selection_{source}_{query}_query_outcome")
            for source in ("swift", "quartz")
            for query in ("cg_on_screen", "cg_all_windows")
        ]
        if any(value in {"invalid_payload", "nil_or_unavailable", None} for value in query_values):
            return "instrumentation_inconsistent"
        failures: list[str] = []
        if any(facts.get(f"selection_{source}_ax_trust") == "not_trusted" for source in ("swift", "quartz")):
            failures.append("accessibility_denied")
        if any(facts.get(f"selection_{source}_screen_capture_preflight") == "denied" for source in ("swift", "quartz")):
            failures.append("screen_capture_denied")
        if (
            facts.get("selection_system_events_automation_preflight") in {"denied", "would_require_consent"}
            or facts.get("selection_system_events_execution_outcome")
            in {"not_authorized", "accessibility_denied", "automation_denied"}
        ):
            failures.append("system_events_denied")
        failures = list(dict.fromkeys(failures))
        if len(failures) > 1:
            return "multiple"
        if failures:
            return failures[0]
        if any(facts.get(f"selection_{source}_on_screen_omission_confirmed") is True for source in ("swift", "quartz")):
            return "on_screen_filter_exclusion"
        if any(int(facts.get(f"selection_{source}_target_rejected_nonzero_layer_count") or 0) > 0 for source in ("swift", "quartz")):
            return "layer_filter_exclusion"
        if any(int(facts.get(f"selection_{source}_target_rejected_nonpositive_geometry_count") or 0) > 0 for source in ("swift", "quartz")):
            return "geometry_filter_exclusion"
        if any(int(facts.get(f"selection_{source}_rejected_target_bundle_mismatch_count") or 0) > 0 for source in ("swift", "quartz")):
            return "identity_correlation_failure"
        permissions_ok = (
            all(facts.get(f"selection_{source}_ax_trust") == "trusted" for source in ("swift", "quartz"))
            and all(facts.get(f"selection_{source}_screen_capture_preflight") == "granted" for source in ("swift", "quartz"))
            and facts.get("selection_system_events_automation_preflight") == "authorized"
            and facts.get("selection_system_events_execution_outcome") == "success"
        )
        all_target_count = sum(
            int(facts.get(f"selection_{source}_all_windows_target_pid_match_count") or 0)
            for source in ("swift", "quartz")
        )
        if permissions_ok:
            if all_target_count > 0:
                return "permissions_ok"
            if any(
                facts.get(f"selection_{source}_cg_all_windows_query_outcome")
                == "success_nonempty_truncated"
                for source in ("swift", "quartz")
            ):
                return "permissions_ok_target_unknown"
            return "permissions_ok_no_target"
        return "unknown"

    @staticmethod
    def _selection_source_permission_outcome(facts: dict[str, Any], source: str) -> str:
        """Reduce permissions for one source without attributing another source's state."""
        if facts.get("selection_permission_request_api_invoked") is True:
            return "forbidden_action_required"
        if source not in {"swift", "quartz", "system_events"}:
            return "not_applicable"
        if source == "system_events":
            preflight = facts.get("selection_system_events_automation_preflight")
            execution = facts.get("selection_system_events_execution_outcome")
            if preflight in {"denied", "would_require_consent"} or execution in {
                "not_authorized", "accessibility_denied", "automation_denied",
            }:
                return "system_events_denied"
            if execution == "skipped_non_authoritative":
                return "skipped_non_authoritative"
            if preflight != "authorized" or execution != "success":
                return "unknown"
            target_count = sum(
                BrowserComputerController._selection_fact_count(
                    facts.get(f"selection_system_events_target_{kind}_match_count")
                )
                for kind in ("name", "pid", "bundle")
            )
            return "permissions_ok" if target_count else "permissions_ok_no_target"

        query_values = [
            facts.get(f"selection_{source}_{query}_query_outcome")
            for query in ("cg_on_screen", "cg_all_windows")
        ]
        if any(value in {"invalid_payload", "nil_or_unavailable", None} for value in query_values):
            return "instrumentation_inconsistent"
        if facts.get(f"selection_{source}_ax_trust") == "not_trusted":
            return "accessibility_denied"
        if facts.get(f"selection_{source}_screen_capture_preflight") == "denied":
            return "screen_capture_denied"
        if facts.get(f"selection_{source}_on_screen_omission_confirmed") is True:
            return "on_screen_filter_exclusion"
        if BrowserComputerController._selection_fact_count(
            facts.get(f"selection_{source}_target_rejected_nonzero_layer_count")
        ) > 0:
            return "layer_filter_exclusion"
        if BrowserComputerController._selection_fact_count(
            facts.get(f"selection_{source}_target_rejected_nonpositive_geometry_count")
        ) > 0:
            return "geometry_filter_exclusion"
        if BrowserComputerController._selection_fact_count(
            facts.get(f"selection_{source}_rejected_target_bundle_mismatch_count")
        ) > 0:
            return "identity_correlation_failure"
        permissions_ok = (
            facts.get(f"selection_{source}_ax_trust") == "trusted"
            and facts.get(f"selection_{source}_screen_capture_preflight") == "granted"
            and all(value in _QUARTZ_QUERY_SUCCESS_OUTCOMES for value in query_values)
        )
        if not permissions_ok:
            return "unknown"
        all_target_count = BrowserComputerController._selection_fact_count(
            facts.get(f"selection_{source}_all_windows_target_pid_match_count")
        )
        if all_target_count > 0:
            return "permissions_ok"
        if facts.get(f"selection_{source}_cg_all_windows_query_outcome") == "success_nonempty_truncated":
            return "permissions_ok_target_unknown"
        return "permissions_ok_no_target"

    @staticmethod
    def _selection_fact_count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _selection_secondary_permission_outcome(
        cls, facts: dict[str, Any], sources: list[str]
    ) -> str:
        if not sources:
            return "not_applicable"
        outcomes = [cls._selection_source_permission_outcome(facts, source) for source in sources]
        if len(outcomes) == 1:
            return outcomes[0]
        benign = {
            "permissions_ok", "permissions_ok_no_target", "permissions_ok_target_unknown",
            "skipped_non_authoritative", "not_applicable",
        }
        failures = list(dict.fromkeys(outcome for outcome in outcomes if outcome not in benign))
        if len(failures) > 1:
            return "multiple"
        if failures:
            return failures[0]
        if "permissions_ok" in outcomes:
            return "permissions_ok"
        if "permissions_ok_target_unknown" in outcomes:
            return "permissions_ok_target_unknown"
        if "permissions_ok_no_target" in outcomes:
            return "permissions_ok_no_target"
        if "skipped_non_authoritative" in outcomes:
            return "skipped_non_authoritative"
        return "unknown"

    def _darwin_window_inventory_observation(self, app: str) -> dict[str, Any]:
        aliases = self._app_alias_tokens(app)
        bundle_aliases: set[str] = set()
        for key, values in _DARWIN_BROWSER_BUNDLE_ID_ALIASES.items():
            if aliases & self._app_alias_tokens(key):
                bundle_aliases.update(str(value).lower() for value in values)
        swift = self._darwin_swift_inventory_observation(
            app=app, aliases=aliases, bundle_aliases=bundle_aliases
        )
        target_pids = set(swift.get("target_pids") or set())
        bundle_pids = set(swift.get("bundle_pids") or set())
        quartz_windows = self._darwin_windows_quartz()
        quartz_permission_facts = self._darwin_quartz_permission_observation(
            app=app, aliases=aliases, target_pids=target_pids, bundle_pids=bundle_pids
        )
        swift_windows = list(swift.get("windows") or [])
        system_observation = self._darwin_system_events_permission_observation(
            app=app,
            enumerate_windows=not bool(swift_windows or quartz_windows),
        )
        system_windows = list(system_observation.get("windows") or [])
        if swift_windows:
            source, windows = "swift_host", swift_windows
        elif quartz_windows:
            source, windows = "quartz", quartz_windows
        elif system_windows:
            source, windows = "system_events", system_windows
        else:
            source, windows = "none", []
        swift_facts = dict(swift.get("facts") or {})
        pid_available = swift_facts.get("selection_target_pid_match_available") is True
        bundle_available = swift_facts.get("selection_target_bundle_match_available") is True
        quartz_facts = self._selection_source_facts(
            "quartz", quartz_windows, app=app,
            observed=quartz_permission_facts.get("selection_quartz_cg_on_screen_query_outcome") in _QUARTZ_QUERY_SUCCESS_OUTCOMES,
            contract_valid=all(
                quartz_permission_facts.get(key) in _QUARTZ_QUERY_SUCCESS_OUTCOMES
                for key in (
                    "selection_quartz_cg_on_screen_query_outcome",
                    "selection_quartz_cg_all_windows_query_outcome",
                )
            ),
            target_pids=target_pids, bundle_pids=bundle_pids,
            pid_match_available=pid_available, bundle_match_available=bundle_available,
            on_screen_only=True, layer_zero=True,
        )
        system_facts = self._selection_source_facts(
            "system_events", system_windows, app=app,
            observed=dict(system_observation.get("facts") or {}).get("selection_system_events_execution_outcome") == "success",
            contract_valid=dict(system_observation.get("facts") or {}).get("selection_system_events_execution_outcome") == "success",
            target_pids=target_pids, bundle_pids=bundle_pids,
            pid_match_available=pid_available, bundle_match_available=bundle_available,
            on_screen_only=False, layer_zero=False,
        )
        facts = {
            **swift_facts, **quartz_facts, **system_facts,
            **quartz_permission_facts, **dict(system_observation.get("facts") or {}),
        }
        source_prefix = {"swift_host": "swift", "quartz": "quartz", "system_events": "system_events"}.get(source)
        later_prefixes = (
            ["quartz", "system_events"] if source == "swift_host"
            else ["system_events"] if source == "quartz" else []
        )
        facts["selection_permission_diagnostic_outcome"] = self._selection_permission_outcome(facts)
        facts["selection_authoritative_permission_source"] = source
        facts["selection_authoritative_permission_outcome"] = self._selection_source_permission_outcome(
            facts, source_prefix or ""
        )
        facts["selection_secondary_permission_outcome"] = self._selection_secondary_permission_outcome(
            facts, later_prefixes
        )
        facts["selection_permission_fact_stability"] = "unknown"
        facts["selection_permission_fact_change_count"] = 0
        primary_target_count = 0
        if source_prefix:
            primary_target_count = sum(
                int(facts.get(f"selection_{source_prefix}_target_{kind}_match_count") or 0)
                for kind in ("name", "pid", "bundle")
            )
        later_target = any(
            int(facts.get(f"selection_{prefix}_target_{kind}_match_count") or 0) > 0
            for prefix in later_prefixes for kind in ("name", "pid", "bundle")
        )
        compared = all(
            facts.get(f"selection_{prefix}_inventory_observed") is True
            for prefix in ("swift", "quartz", "system_events")
        )
        consistent = all(
            int(facts.get(f"selection_{prefix}_usable_window_count") or 0)
            <= int(facts.get(f"selection_{prefix}_window_total_count") or 0)
            for prefix in ("swift", "quartz", "system_events")
        ) and (
            facts.get("selection_swift_helper_response_contract") in {"valid_success", "valid_error"}
            and facts.get("selection_swift_helper_contract_version_class") == "expected"
            and facts.get("selection_swift_inventory_contract_valid") is True
        )
        facts.update({
            "selection_requested_alias_valid": bool(aliases),
            "selection_requested_bundle_alias_available": bool(bundle_aliases),
            "selection_inventory_source_used": source,
            "selection_primary_source_nonempty": bool(windows),
            "selection_later_sources_suppressed_by_selection_policy": source in {"swift_host", "quartz"},
            "selection_diagnostic_sources_compared": compared,
            "selection_primary_source_target_match_absent": primary_target_count == 0,
            "selection_later_source_target_match_present": later_target,
            "selection_primary_source_suppressed_target_observation": primary_target_count == 0 and later_target,
            "selection_inventory_instrumentation_consistent": consistent,
        })
        causes: list[str] = []
        helper_contract = facts.get("selection_swift_helper_response_contract")
        helper_version = facts.get("selection_swift_helper_contract_version_class")
        helper_issue = False
        if helper_contract not in {"valid_success", "valid_error"}:
            causes.append("helper_unavailable" if helper_contract == "not_invoked" else "helper_contract_invalid")
            helper_issue = True
        elif helper_version != "expected":
            causes.append("helper_contract_invalid")
            helper_issue = True
        if not consistent:
            causes.append("instrumentation_inconsistent")
        process_present = facts.get("selection_nsworkspace_target_process_present") is True
        all_target_count = sum(
            int(facts.get(f"selection_{prefix}_target_{kind}_match_count") or 0)
            for prefix in ("swift", "quartz", "system_events") for kind in ("name", "pid", "bundle")
        )
        if not helper_issue:
            if process_present and all_target_count == 0:
                causes.append("process_present_no_window")
            elif not process_present and all_target_count == 0:
                causes.append("process_absent")
            if primary_target_count == 0 and later_target:
                causes.append("primary_source_divergence")
            if source_prefix and int(facts.get(f"selection_{source_prefix}_target_name_match_count") or 0) == 0 and any(
                int(facts.get(f"selection_{source_prefix}_target_{kind}_match_count") or 0) > 0
                for kind in ("pid", "bundle")
            ):
                causes.append("owner_name_mismatch")
        unique_causes = list(dict.fromkeys(causes))
        outcome = unique_causes[0] if len(unique_causes) == 1 else "multiple" if unique_causes else "unknown"
        stage = (
            "helper_resolution" if helper_issue
            else "source_comparison" if not consistent
            else "complete"
        )
        facts.update({
            "selection_inventory_diagnostic_stage": stage,
            "selection_inventory_diagnostic_outcome": outcome,
            "selection_inventory_cause_count": min(4, len(unique_causes)),
        })
        selected_identity_observation = swift.get("_selected_identity_observation")
        if source != "swift_host" or not isinstance(selected_identity_observation, dict):
            selected_identity_observation = None
        return {
            "windows": windows,
            "facts": facts,
            # The native-only mapping is retained only for a Swift-authoritative
            # inventory and consumed before _select_window returns.
            "_selected_identity_observation": selected_identity_observation,
        }

    def _darwin_windows(self) -> list[dict[str, Any]]:
        swift_windows = self._darwin_swift_windows()
        if swift_windows:
            return swift_windows
        quartz_windows = self._darwin_windows_quartz()
        if quartz_windows:
            return quartz_windows
        return self._darwin_windows_system_events()

    def _darwin_windows_system_events(self) -> list[dict[str, Any]]:
        script = r'''
tell application "System Events"
  set output to ""
  repeat with proc in (application processes whose background only is false)
    set procName to name of proc
    set procPid to unix id of proc
    set procFront to frontmost of proc
    repeat with win in windows of proc
      try
        set winName to name of win
        set winPos to position of win
        set winSize to size of win
        set output to output & procName & tab & winName & tab & (item 1 of winPos) & tab & (item 2 of winPos) & tab & (item 1 of winSize) & tab & (item 2 of winSize) & tab & procFront & tab & procPid & linefeed
      end try
    end repeat
  end repeat
  return output
end tell
'''
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
        except Exception:
            return []
        windows: list[dict[str, Any]] = []
        for line in (completed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            try:
                window = {
                    "app": parts[0],
                    "title": parts[1],
                    "x": int(float(parts[2])),
                    "y": int(float(parts[3])),
                    "width": int(float(parts[4])),
                    "height": int(float(parts[5])),
                    "active": parts[6].strip().lower() == "true",
                }
                if len(parts) >= 8:
                    window["pid"] = int(parts[7])
            except Exception:
                continue
            if window["width"] > 0 and window["height"] > 0:
                windows.append(window)
        return windows

    @staticmethod
    def _frontmost_app_name() -> str:
        try:
            completed = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to get name of first application process whose frontmost is true',
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
            return (completed.stdout or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _darwin_swift_host_result(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            from ..computer.mac.swift_host import MacSwiftComputerHost

            host = MacSwiftComputerHost()
            if not host.available():
                return None
            result = host.run(action, dict(payload or {}))
            return result if isinstance(result, dict) and not result.get("is_error") else None
        except Exception:
            return None

    @staticmethod
    def _darwin_swift_optional_action_result(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if platform.system() != "Darwin":
            return None
        try:
            from ..computer.mac.swift_host import MacSwiftComputerHost

            host = MacSwiftComputerHost()
            if not host.available():
                return None
            result = host.run(action, dict(payload or {}))
            if not isinstance(result, dict):
                return {"action": action, "result": result}
            if result.get("is_error"):
                code = str(result.get("error_code") or "").strip().upper()
                reason = str(result.get("reason") or "")
                if code == "UNSUPPORTED_ACTION" or reason.startswith("Unsupported macOS computer action"):
                    return None
            return dict(result)
        except Exception:
            return None

    def _darwin_swift_windows(self) -> list[dict[str, Any]]:
        result = self._darwin_swift_host_result("computer.windows")
        windows = result.get("windows") if isinstance(result, dict) else None
        return [item for item in windows if isinstance(item, dict)] if isinstance(windows, list) else []

    def _darwin_swift_apps(self) -> list[dict[str, Any]]:
        result = self._darwin_swift_host_result("computer.apps")
        apps = result.get("apps") if isinstance(result, dict) else None
        return [item for item in apps if isinstance(item, dict)] if isinstance(apps, list) else []

    def _darwin_swift_activate_app(self, app_name: str = "", *, pid: Any = None, bundle_id: str = "") -> bool:
        payload: dict[str, Any] = {}
        if app_name:
            payload["app"] = app_name
        if pid not in (None, ""):
            payload["pid"] = pid
        if bundle_id:
            payload["bundle_id"] = bundle_id
        result = self._darwin_swift_host_result("computer.activate_app", payload)
        return bool(result and result.get("executed") and result.get("active"))

    def _darwin_running_apps(self) -> list[dict[str, Any]]:
        script = r'''
tell application "System Events"
  set output to ""
  repeat with proc in (application processes whose background only is false)
    try
      set procName to name of proc
      set procPid to unix id of proc
      set procFront to frontmost of proc
      set winCount to count of windows of proc
      set output to output & procName & tab & procPid & tab & procFront & tab & winCount & linefeed
    end try
  end repeat
  return output
end tell
'''
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
        except Exception:
            return []
        apps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in (completed.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name = parts[0].strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            try:
                pid = int(parts[1])
            except Exception:
                pid = None
            try:
                window_count = int(parts[3])
            except Exception:
                window_count = 0
            app = {
                "name": name,
                "app": name,
                "running": True,
                "active": parts[2].strip().lower() == "true",
                "window_count": window_count,
                "has_windows": window_count > 0,
            }
            if pid is not None:
                app["pid"] = pid
            apps.append(app)
        return apps

    @staticmethod
    def _darwin_installed_apps(*, limit: int = 300) -> list[dict[str, Any]]:
        roots = [
            Path("/Applications"),
            Path.home() / "Applications",
            Path("/System/Applications"),
        ]
        apps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            try:
                candidates = sorted(root.glob("*.app"), key=lambda path: path.name.lower())
            except Exception:
                continue
            for path in candidates:
                name = path.stem.strip()
                key = str(path).lower()
                if not name or key in seen:
                    continue
                seen.add(key)
                apps.append({"name": name, "app": name, "path": str(path), "source": str(root), "running": False})
                if len(apps) >= limit:
                    return apps
        return apps

    def _activate_app_name(self, app_name: str) -> bool:
        app_name = app_name.strip()
        if not app_name:
            return False
        system = platform.system()
        if system == "Darwin":
            if self._darwin_swift_activate_app(app_name):
                return True
            script = """
tell application "System Events"
  set appNeedle to %s
  repeat with candidateProc in (application processes whose background only is false)
    try
      if ((name of candidateProc) contains appNeedle) then
        set frontmost of candidateProc to true
        return "activated"
      end if
    end try
  end repeat
end tell
try
  tell application appNeedle to activate
  return "activated"
end try
return "not_found"
""" % json.dumps(app_name)
            try:
                completed = subprocess.run(
                    ["osascript", "-e", script],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
                )
                return "activated" in (completed.stdout or "")
            except Exception:
                return False
        if system == "Windows":
            name = self._ps_single(app_name)
            script = "\n".join(
                [
                    "Add-Type -AssemblyName Microsoft.VisualBasic",
                    f"[void][Microsoft.VisualBasic.Interaction]::AppActivate('{name}')",
                ]
            )
            try:
                self._run_powershell(script)
                return True
            except Exception:
                return False
        if system == "Linux":
            try:
                from ..computer.linux import xdotool

                window = xdotool.find_window(app=app_name)
                return xdotool.activate_window(window)
            except Exception:
                return False
        return False

    def _launch_app(self, app: dict[str, Any]) -> bool:
        path = str(app.get("path") or "").strip()
        name = str(app.get("name") or app.get("app") or "").strip()
        system = platform.system()
        try:
            if system == "Darwin":
                if path:
                    subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                if name:
                    subprocess.Popen(["open", "-a", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
            if system == "Windows" and path:
                self._run_powershell(f"Start-Process -FilePath '{self._ps_single(path)}'")
                return True
            if system == "Linux":
                command = str(app.get("exec") or "").strip()
                if command:
                    executable = command.split()[0]
                    subprocess.Popen([executable], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
        except Exception:
            return False
        return False

    def _darwin_windows_quartz(self) -> list[dict[str, Any]]:
        code = "MAX_BRIDGED_RECORDS = " + str(_QUARTZ_BRIDGE_MAX_ITEMS) + r"""
import json
import Quartz

def has_mapping_capability(value):
    return any(callable(getattr(value, name, None)) for name in (
        "get", "objectForKey_", "__getitem__",
    ))

def mapping_get(value, key, default=None):
    getter = getattr(value, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            try:
                result = getter(key)
                return default if result is None else result
            except Exception:
                pass
        except Exception:
            pass
    object_for_key = getattr(value, "objectForKey_", None)
    if callable(object_for_key):
        try:
            result = object_for_key(key)
            return default if result is None else result
        except Exception:
            pass
    get_item = getattr(value, "__getitem__", None)
    if callable(get_item):
        try:
            return get_item(key)
        except Exception:
            pass
    return default

def bounded_iterable(value):
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        iterator = iter(value)
    except Exception:
        count = getattr(value, "count", None)
        item_at_index = getattr(value, "objectAtIndex_", None)
        if not callable(count) or not callable(item_at_index):
            return None
        try:
            size = max(0, int(count()))
            if size > MAX_BRIDGED_RECORDS:
                return None
            return [item_at_index(index) for index in range(size)]
        except Exception:
            return None
    records = []
    for _ in range(MAX_BRIDGED_RECORDS):
        try:
            records.append(next(iterator))
        except StopIteration:
            return records
        except Exception:
            return None
    try:
        next(iterator)
    except StopIteration:
        return records
    except Exception:
        return None
    return None

def number(value):
    try:
        return int(value or 0)
    except Exception:
        return 0

def decimal(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0

options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
items = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
items = bounded_iterable(items)
if items is None or any(not has_mapping_capability(item) for item in items):
    raise ValueError("Quartz window bridge contract invalid")
raw = []
for item in items:
    if number(mapping_get(item, "kCGWindowLayer")) != 0:
        continue
    bounds = mapping_get(item, "kCGWindowBounds")
    if not has_mapping_capability(bounds):
        continue
    width = int(round(decimal(mapping_get(bounds, "Width"))))
    height = int(round(decimal(mapping_get(bounds, "Height"))))
    if width <= 0 or height <= 0:
        continue
    raw.append({
        "app": str(mapping_get(item, "kCGWindowOwnerName") or ""),
        "pid": number(mapping_get(item, "kCGWindowOwnerPID")),
        "title": str(mapping_get(item, "kCGWindowName") or ""),
        "x": int(round(decimal(mapping_get(bounds, "X")))),
        "y": int(round(decimal(mapping_get(bounds, "Y")))),
        "width": width,
        "height": height,
        "window_id": number(mapping_get(item, "kCGWindowNumber")),
    })

def overlap(a1, a2, b1, b2):
    return max(0, min(a2, b2) - max(a1, b1))

def union_rect(rects):
    left = min(rect["x"] for rect in rects)
    top = min(rect["y"] for rect in rects)
    right = max(rect["x"] + rect["width"] for rect in rects)
    bottom = max(rect["y"] + rect["height"] for rect in rects)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}

used_ids = set()
windows = []
for item in raw:
    if not str(item.get("title") or ""):
        continue
    item_left = item["x"]
    item_right = item["x"] + item["width"]
    item_top = item["y"]
    peers = [item]
    for peer in raw:
        if peer is item:
            continue
        if peer.get("app") != item.get("app"):
            continue
        if str(peer.get("title") or ""):
            continue
        if peer.get("width", 0) < item["width"] * 0.6:
            continue
        peer_left = peer["x"]
        peer_right = peer["x"] + peer["width"]
        horizontal = overlap(item_left, item_right, peer_left, peer_right)
        if horizontal < min(item["width"], peer["width"]) * 0.75:
            continue
        peer_top = peer["y"]
        peer_bottom = peer["y"] + peer["height"]
        near_content_top = peer_top <= item_top + 80 and peer_bottom >= item_top - 180
        if not near_content_top:
            continue
        peers.append(peer)
    if len(peers) > 1:
        rect = union_rect(peers)
        composite = dict(item)
        composite.update(rect)
        composite["content_rect"] = {
            "x": item["x"],
            "y": item["y"],
            "width": item["width"],
            "height": item["height"],
        }
        composite["capture_rect"] = rect
        composite["capture_method"] = "rect"
        composite["frame_window_ids"] = [
            int(peer.get("window_id", 0) or 0)
            for peer in peers
            if peer.get("window_id")
        ]
        windows.append(composite)
        used_ids.update(composite["frame_window_ids"])
    else:
        windows.append(item)
        if item.get("window_id"):
            used_ids.add(int(item["window_id"]))

for item in raw:
    if item.get("window_id") and int(item["window_id"]) in used_ids:
        continue
    windows.append(item)
print(json.dumps(windows))
"""
        try:
            completed = subprocess.run(
                _current_python_snippet_command(code),
                check=True,
                capture_output=True,
                text=True,
                timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
            )
            windows = json.loads(completed.stdout or "[]")
        except Exception:
            return []
        frontmost = self._frontmost_app_name().lower()
        normalized = []
        for item in windows:
            window = self._normalize_window_record(item)
            if window is None:
                continue
            window["active"] = bool(frontmost and str(window.get("app") or "").lower() == frontmost)
            normalized.append(window)
        return normalized

    def _focus_window(self, window: dict[str, Any]) -> None:
        raw_app = str(window.get("app") or "")
        raw_title = str(window.get("title") or "")
        app = raw_app.replace('"', '\\"')
        if not app:
            return
        if platform.system() == "Darwin":
            if self._darwin_swift_activate_app(raw_app):
                return
            script = """
tell application "System Events"
  set appName to %s
  set titleNeedle to %s
  tell application process appName
    set frontmost to true
    if titleNeedle is not "" then
      repeat with candidateWindow in windows
        try
          if (name of candidateWindow) contains titleNeedle then
            perform action "AXRaise" of candidateWindow
            exit repeat
          end if
        end try
      end repeat
    end if
  end tell
end tell
""" % (json.dumps(raw_app), json.dumps(raw_title))
            try:
                subprocess.run(
                    ["osascript", "-e", script],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        elif platform.system() == "Windows":
            try:
                hwnd = int(window.get("window_id") or 0)
            except Exception:
                hwnd = 0
            if hwnd <= 0:
                return
            script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class RumiWindowFocus {{
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}}
'@ -ErrorAction SilentlyContinue
$hwnd = [IntPtr]{hwnd}
[void][RumiWindowFocus]::ShowWindowAsync($hwnd, 9)
[void][RumiWindowFocus]::SetForegroundWindow($hwnd)
"""
            try:
                self._run_powershell(script)
            except Exception:
                pass
        elif platform.system() == "Linux":
            try:
                from ..computer.linux import xdotool

                xdotool.activate_window(window)
            except Exception:
                pass

    def _darwin_move_cursor(self, payload: dict[str, Any]) -> None:
        x = int(payload.get("x", 0))
        y = int(payload.get("y", 0))
        cliclick = shutil.which("cliclick")
        if cliclick:
            subprocess.run([cliclick, f"m:{x},{y}"], check=True)
            return
        code = (
            "import Quartz, sys\n"
            f"Quartz.CGWarpMouseCursorPosition(({x}, {y}))\n"
            "Quartz.CGAssociateMouseAndMouseCursorPosition(True)\n"
        )
        try:
            subprocess.run(_current_python_snippet_command(code), check=True, timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS)
        except Exception as exc:
            raise RuntimeError("computer.move requires cliclick or PyObjC Quartz on macOS") from exc

    def _darwin_click(self, payload: dict[str, Any]) -> None:
        x = int(payload.get("x", 0))
        y = int(payload.get("y", 0))
        button = str(payload.get("button") or "left").lower()
        cliclick = shutil.which("cliclick")
        if cliclick:
            prefix = "rc" if button in {"right", "secondary"} else "c"
            subprocess.run([cliclick, f"{prefix}:{x},{y}"], check=True)
            return
        button_index = 1 if button in {"right", "secondary"} else 0
        down_event = "kCGEventRightMouseDown" if button_index == 1 else "kCGEventLeftMouseDown"
        up_event = "kCGEventRightMouseUp" if button_index == 1 else "kCGEventLeftMouseUp"
        code = (
            "import Quartz\n"
            f"point = Quartz.CGPoint({x}, {y})\n"
            f"down = Quartz.CGEventCreateMouseEvent(None, Quartz.{down_event}, point, {button_index})\n"
            f"up = Quartz.CGEventCreateMouseEvent(None, Quartz.{up_event}, point, {button_index})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)\n"
        )
        try:
            subprocess.run(_current_python_snippet_command(code), check=True, timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS)
        except Exception:
            swift = shutil.which("swift")
            if swift:
                mouse_button = "right" if button_index == 1 else "left"
                mouse_down = "rightMouseDown" if button_index == 1 else "leftMouseDown"
                mouse_up = "rightMouseUp" if button_index == 1 else "leftMouseUp"
                swift_code = (
                    "import CoreGraphics\n"
                    f"let point = CGPoint(x: {x}, y: {y})\n"
                    f"let down = CGEvent(mouseEventSource: nil, mouseType: .{mouse_down}, mouseCursorPosition: point, mouseButton: .{mouse_button})\n"
                    f"let up = CGEvent(mouseEventSource: nil, mouseType: .{mouse_up}, mouseCursorPosition: point, mouseButton: .{mouse_button})\n"
                    "down?.post(tap: .cghidEventTap)\n"
                    "up?.post(tap: .cghidEventTap)\n"
                )
                try:
                    subprocess.run([swift, "-e", swift_code], check=True, timeout=_DARWIN_CGEVENT_TIMEOUT_SECONDS)
                    return
                except Exception:
                    pass
            script = self._apple_script("computer.click", payload)
            subprocess.run(["osascript", "-e", script], check=True, timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS)

    def _darwin_drag(self, payload: dict[str, Any]) -> None:
        x1 = int(payload.get("x1", payload.get("x", 0)))
        y1 = int(payload.get("y1", payload.get("y", 0)))
        x2 = int(payload.get("x2", payload.get("x", 0)))
        y2 = int(payload.get("y2", payload.get("y", 0)))
        button = str(payload.get("button") or "left").lower()
        button_index = 1 if button in {"right", "secondary"} else 0
        down_event = "kCGEventRightMouseDown" if button_index == 1 else "kCGEventLeftMouseDown"
        drag_event = "kCGEventRightMouseDragged" if button_index == 1 else "kCGEventLeftMouseDragged"
        up_event = "kCGEventRightMouseUp" if button_index == 1 else "kCGEventLeftMouseUp"
        code = (
            "import Quartz, time\n"
            f"start = Quartz.CGPoint({x1}, {y1})\n"
            f"end = Quartz.CGPoint({x2}, {y2})\n"
            f"down = Quartz.CGEventCreateMouseEvent(None, Quartz.{down_event}, start, {button_index})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)\n"
            "time.sleep(0.05)\n"
            "steps = 8\n"
            "for index in range(1, steps + 1):\n"
            "    px = start.x + (end.x - start.x) * index / steps\n"
            "    py = start.y + (end.y - start.y) * index / steps\n"
            f"    drag = Quartz.CGEventCreateMouseEvent(None, Quartz.{drag_event}, Quartz.CGPoint(px, py), {button_index})\n"
            "    Quartz.CGEventPost(Quartz.kCGHIDEventTap, drag)\n"
            "    time.sleep(0.02)\n"
            f"up = Quartz.CGEventCreateMouseEvent(None, Quartz.{up_event}, end, {button_index})\n"
            "Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)\n"
        )
        try:
            subprocess.run(_current_python_snippet_command(code), check=True, timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS)
        except Exception as exc:
            raise RuntimeError("computer.drag requires PyObjC Quartz on macOS") from exc

    def _darwin_type(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        command = ["osascript", "-e", self._darwin_clipboard_paste_script(), "--", text]
        subprocess.run(command, check=True, timeout=_DARWIN_AUTOMATION_TIMEOUT_SECONDS)

    @staticmethod
    def _darwin_clipboard_paste_script() -> str:
        return """
on run argv
set rumiPasteText to item 1 of argv
set rumiOriginalClipboard to missing value
set rumiHadClipboard to false
try
  set rumiOriginalClipboard to the clipboard
  set rumiHadClipboard to true
end try
try
  set the clipboard to rumiPasteText
  delay 0.05
  tell application "System Events" to keystroke "v" using {command down}
  delay 0.05
on error pasteErrorMessage number pasteErrorNumber
  if rumiHadClipboard then
    set the clipboard to rumiOriginalClipboard
  else
    set the clipboard to ""
  end if
  error pasteErrorMessage number pasteErrorNumber
end try
if rumiHadClipboard then
  set the clipboard to rumiOriginalClipboard
else
  set the clipboard to ""
end if
end run
"""

    def _apple_script(self, action: str, payload: dict[str, Any]) -> str:
        if action == "computer.click":
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            return f'tell application "System Events" to click at {{{x}, {y}}}'
        if action == "computer.type":
            text = json.dumps(str(payload.get("text", "")), ensure_ascii=False)
            return f'tell application "System Events" to keystroke {text}'
        if action == "computer.key":
            key = payload.get("key", "return")
            modifiers = payload.get("modifiers")
            if not isinstance(modifiers, list):
                modifier = payload.get("modifier")
                modifiers = [modifier] if modifier else []
            combo_parts = [part.strip() for part in str(payload.get("key_combo") or "").split("+") if part.strip()]
            if combo_parts:
                modifiers = combo_parts[:-1] + modifiers
                key = combo_parts[-1]
            key = _normalize_key_name(key)
            using = self._apple_script_modifiers(modifiers)
            if isinstance(key, int):
                return self._repeat_apple_script(
                    f'tell application "System Events" to key code {key}{using}',
                    _key_press_count(payload),
                )
            normalized = str(key).strip().lower()
            key_codes = {
                "return": 36,
                "enter": 36,
                "tab": 48,
                "escape": 53,
                "esc": 53,
                "backspace": 51,
                "delete": 51,
                "del": 51,
                "forward_delete": 117,
                "up": 126,
                "down": 125,
                "left": 123,
                "right": 124,
                "space": 49,
            }
            if normalized in key_codes:
                command = f'tell application "System Events" to key code {key_codes[normalized]}{using}'
            else:
                command = f'tell application "System Events" to keystroke {json.dumps(str(key), ensure_ascii=False)}{using}'
            return self._repeat_apple_script(command, _key_press_count(payload))
        if action == "computer.scroll":
            amount = int(payload.get("amount", 1))
            return f'tell application "System Events" to scroll wheel {amount}'
        raise ValueError(action)

    @staticmethod
    def _repeat_apple_script(command: str, count: int) -> str:
        count = max(1, min(200, int(count or 1)))
        if count == 1:
            return command
        escaped = json.dumps(command)
        return (
            f"repeat {count} times\n"
            f"  run script {escaped}\n"
            "end repeat"
        )

    @staticmethod
    def _apple_script_modifiers(modifiers: list[Any]) -> str:
        names: list[str] = []
        for item in modifiers:
            normalized = str(item or "").strip().lower()
            if normalized in {"command", "cmd", "meta", "super"}:
                names.append("command down")
            elif normalized in {"shift"}:
                names.append("shift down")
            elif normalized in {"option", "alt"}:
                names.append("option down")
            elif normalized in {"control", "ctrl"}:
                names.append("control down")
        if not names:
            return ""
        return " using {" + ", ".join(dict.fromkeys(names)) + "}"

    def _windows_running_apps(self) -> list[dict[str, Any]]:
        script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class RumiDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@ -ErrorAction SilentlyContinue
[void][RumiDpi]::SetProcessDPIAware()
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class RumiActiveApp {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
$front = [RumiActiveApp]::GetForegroundWindow()
$items = Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object {
  [pscustomobject]@{
    name = $_.ProcessName
    app = $_.ProcessName
    pid = $_.Id
    title = $_.MainWindowTitle
    running = $true
    active = ($_.MainWindowHandle -eq $front)
    window_count = 1
    has_windows = $true
  }
}
$items | ConvertTo-Json -Compress
'''
        try:
            return [self._normalize_app_record(item) for item in self._json_list(self._run_powershell_capture(script))]
        except Exception:
            return []

    def _windows_installed_apps(self, *, limit: int = 300) -> list[dict[str, Any]]:
        script = r'''
$ErrorActionPreference = 'SilentlyContinue'
$limit = %d
$roots = @(
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
  "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
  "$env:ProgramFiles",
  "${env:ProgramFiles(x86)}"
) | Where-Object { $_ -and (Test-Path $_) }
$items = @()
foreach ($root in $roots) {
  $items += Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @('.lnk', '.exe') } |
    Select-Object -First $limit |
    ForEach-Object {
      [pscustomobject]@{
        name = $_.BaseName
        app = $_.BaseName
        path = $_.FullName
        source = $root
        running = $false
      }
    }
  if ($items.Count -ge $limit) { break }
}
$items | Select-Object -First $limit | ConvertTo-Json -Compress
''' % limit
        try:
            return [self._normalize_app_record(item) for item in self._json_list(self._run_powershell_capture(script))]
        except Exception:
            return []

    @staticmethod
    def _windows_open_url_foreground(url: str, app_name: str) -> bool:
        normalized = app_name.strip().lower()
        candidates: list[tuple[Path, bool]] = []
        roots = [os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")]
        for root_value in roots:
            if not root_value:
                continue
            root = Path(root_value)
            if any(name in normalized for name in ("chrome", "chromium")):
                candidates.append((root / "Google" / "Chrome" / "Application" / "chrome.exe", False))
            if "edge" in normalized:
                candidates.append((root / "Microsoft" / "Edge" / "Application" / "msedge.exe", False))
            if "firefox" in normalized:
                candidates.append((root / "Mozilla Firefox" / "firefox.exe", False))
        for name in (app_name, f"{app_name}.exe"):
            resolved = shutil.which(name)
            if resolved:
                candidates.insert(0, (Path(resolved), True))
        for executable, resolved_from_path in candidates:
            if not resolved_from_path and not executable.exists():
                continue
            try:
                subprocess.Popen([str(executable), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _windows_dpi_awareness_script() -> str:
        return r'''
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class RumiDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@ -ErrorAction SilentlyContinue
[void][RumiDpi]::SetProcessDPIAware()
'''

    def _windows_screenshot(self, path: Path, target: dict[str, Any] | None = None) -> dict[str, Any]:
        escaped = self._ps_single(str(path))
        bounds_script = (
            "$bounds = New-Object System.Drawing.Rectangle({}, {}, {}, {})".format(
                int(target.get("x", 0)),
                int(target.get("y", 0)),
                int(target.get("width", 0)),
                int(target.get("height", 0)),
            )
            if target
            else "$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen"
        )
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Windows.Forms",
                "Add-Type -AssemblyName System.Drawing",
                self._windows_dpi_awareness_script(),
                bounds_script,
                "$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height",
                "$graphics = [System.Drawing.Graphics]::FromImage($bitmap)",
                "$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)",
                f"$bitmap.Save('{escaped}', [System.Drawing.Imaging.ImageFormat]::Png)",
                "$graphics.Dispose()",
                "$bitmap.Dispose()",
            ]
        )
        self._run_powershell(script)
        if target:
            return {
                "x": int(target.get("x", 0)),
                "y": int(target.get("y", 0)),
                "width": int(target.get("width", 0)),
                "height": int(target.get("height", 0)),
                "screen": "selected_window",
                "unit": "display_coordinate",
            }
        return self._windows_virtual_screen_bounds()

    def _windows_virtual_screen_bounds(self) -> dict[str, Any]:
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "Add-Type -AssemblyName System.Windows.Forms",
                self._windows_dpi_awareness_script(),
                "$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen",
                "ConvertTo-Json @{ x = [int]$bounds.X; y = [int]$bounds.Y; width = [int]$bounds.Width; height = [int]$bounds.Height; screen = 'virtual_screen'; unit = 'display_coordinate' } -Compress",
            ]
        )
        try:
            value = json.loads(self._run_powershell_capture(script) or "{}")
            if isinstance(value, dict) and value.get("width") and value.get("height"):
                return {
                    "x": int(value.get("x", 0)),
                    "y": int(value.get("y", 0)),
                    "width": int(value.get("width", 0)),
                    "height": int(value.get("height", 0)),
                    "screen": "virtual_screen",
                    "unit": "display_coordinate",
                }
        except Exception:
            pass
        return {"x": 0, "y": 0, "width": 0, "height": 0, "screen": "virtual_screen", "unit": "display_coordinate"}

    def _windows_desktop_action(self, action: str, payload: dict[str, Any]) -> None:
        prelude = [
            "$ErrorActionPreference = 'Stop'",
            "Add-Type -AssemblyName System.Windows.Forms",
            "Add-Type -AssemblyName System.Drawing",
            self._windows_dpi_awareness_script(),
        ]
        if action == "computer.move":
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            self._run_powershell("\n".join(prelude + [f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x}, {y})"]))
            return
        if action == "computer.click":
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            restore_cursor = payload.get("isolate_cursor", True) is not False
            script = "\n".join(
                prelude
                + [
                    "Add-Type -TypeDefinition @'\nusing System;\nusing System.Runtime.InteropServices;\npublic class RumiMouse {\n  [DllImport(\"user32.dll\")]\n  public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);\n}\n'@",
                    "$original = [System.Windows.Forms.Cursor]::Position",
                    f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x}, {y})",
                    "[RumiMouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)",
                    "[RumiMouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)",
                    "[System.Windows.Forms.Cursor]::Position = $original" if restore_cursor else "",
                ]
            )
            self._run_powershell(script)
            return
        if action == "computer.drag":
            x1 = int(payload.get("x1", payload.get("x", 0)))
            y1 = int(payload.get("y1", payload.get("y", 0)))
            x2 = int(payload.get("x2", payload.get("x", 0)))
            y2 = int(payload.get("y2", payload.get("y", 0)))
            restore_cursor = payload.get("isolate_cursor", True) is not False
            script = "\n".join(
                prelude
                + [
                    "Add-Type -TypeDefinition @'\nusing System;\nusing System.Runtime.InteropServices;\npublic class RumiMouse {\n  [DllImport(\"user32.dll\")]\n  public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);\n}\n'@",
                    "$original = [System.Windows.Forms.Cursor]::Position",
                    f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x1}, {y1})",
                    "[RumiMouse]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)",
                    "$steps = 12",
                    "for ($index = 1; $index -le $steps; $index++) {",
                    f"  $px = [int]({x1} + (({x2} - {x1}) * $index / $steps))",
                    f"  $py = [int]({y1} + (({y2} - {y1}) * $index / $steps))",
                    "  [System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point($px, $py)",
                    "  Start-Sleep -Milliseconds 15",
                    "}",
                    "[RumiMouse]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)",
                    "[System.Windows.Forms.Cursor]::Position = $original" if restore_cursor else "",
                ]
            )
            self._run_powershell(script)
            return
        if action == "computer.type":
            text = self._ps_single(self._windows_sendkeys_escape_text(str(payload.get("text", ""))))
            self._run_powershell("\n".join(prelude + [f"[System.Windows.Forms.SendKeys]::SendWait('{text}')"]))
            return
        if action == "computer.key":
            key_combo = _key_combo_from_payload({**payload, "key": payload.get("key", "ENTER")})
            key = self._ps_single(self._windows_send_key(key_combo, None))
            count = _key_press_count(payload)
            self._run_powershell(
                "\n".join(
                    prelude
                    + [
                        f"$key = '{key}'",
                        f"for ($i = 0; $i -lt {count}; $i++) {{ [System.Windows.Forms.SendKeys]::SendWait($key) }}",
                    ]
                )
            )
            return
        if action == "computer.scroll":
            amount = int(payload.get("amount", 1))
            wheel_delta = amount * 120
            has_point = any(key in payload for key in ("x", "y", "point", "coordinate", "coordinates"))
            point_payload = payload
            if has_point:
                point_payload, _marker = self._resolve_action_point(payload, remember_cursor=False)
            script = "\n".join(
                prelude
                + [
                    "Add-Type -TypeDefinition @'\nusing System;\nusing System.Runtime.InteropServices;\npublic class RumiMouse {\n  [DllImport(\"user32.dll\")]\n  public static extern void mouse_event(uint flags, int dx, int dy, int data, UIntPtr extra);\n}\n'@",
                    "$original = [System.Windows.Forms.Cursor]::Position" if has_point else "",
                    (
                        "[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({}, {})".format(
                            int(point_payload.get("x", 0)),
                            int(point_payload.get("y", 0)),
                        )
                        if has_point
                        else ""
                    ),
                    f"[RumiMouse]::mouse_event(0x0800, 0, 0, {wheel_delta}, [UIntPtr]::Zero)",
                    "[System.Windows.Forms.Cursor]::Position = $original" if has_point else "",
                ]
            )
            self._run_powershell(script)
            return
        raise ValueError(action)

    def _windows_windows(self) -> list[dict[str, Any]]:
        script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public class RumiDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@ -ErrorAction SilentlyContinue
[void][RumiDpi]::SetProcessDPIAware()
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public class RumiWindowEnum {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
'@
$front = [RumiWindowEnum]::GetForegroundWindow()
$items = New-Object System.Collections.Generic.List[object]
$callback = [RumiWindowEnum+EnumWindowsProc]{
  param([IntPtr]$hWnd, [IntPtr]$lParam)
  if (-not [RumiWindowEnum]::IsWindowVisible($hWnd)) { return $true }
  $titleBuilder = New-Object System.Text.StringBuilder 512
  [void][RumiWindowEnum]::GetWindowText($hWnd, $titleBuilder, $titleBuilder.Capacity)
  $title = $titleBuilder.ToString()
  if ([string]::IsNullOrWhiteSpace($title)) { return $true }
  $rect = New-Object RumiWindowEnum+RECT
  if (-not [RumiWindowEnum]::GetWindowRect($hWnd, [ref]$rect)) { return $true }
  $width = $rect.Right - $rect.Left
  $height = $rect.Bottom - $rect.Top
  if ($width -le 0 -or $height -le 0) { return $true }
  [uint32]$procId = 0
  [void][RumiWindowEnum]::GetWindowThreadProcessId($hWnd, [ref]$procId)
  $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
  $items.Add([pscustomobject]@{
    app = if ($proc) { $proc.ProcessName } else { "" }
    title = $title
    x = $rect.Left
    y = $rect.Top
    width = $width
    height = $height
    active = ($hWnd -eq $front)
    window_id = $hWnd.ToInt64()
  })
  return $true
}
[void][RumiWindowEnum]::EnumWindows($callback, [IntPtr]::Zero)
$items | ConvertTo-Json -Compress
'''
        try:
            return [window for window in (self._normalize_window_record(item) for item in self._json_list(self._run_powershell_capture(script))) if window]
        except Exception:
            return []

    def _windows_active_window(self) -> dict[str, Any] | None:
        script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class RumiDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@ -ErrorAction SilentlyContinue
[void][RumiDpi]::SetProcessDPIAware()
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public class RumiWindow {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
'@
$h = [RumiWindow]::GetForegroundWindow()
$r = New-Object RumiWindow+RECT
[void][RumiWindow]::GetWindowRect($h, [ref]$r)
$titleBuilder = New-Object System.Text.StringBuilder 512
[void][RumiWindow]::GetWindowText($h, $titleBuilder, $titleBuilder.Capacity)
[uint32]$procId = 0
[void][RumiWindow]::GetWindowThreadProcessId($h, [ref]$procId)
$proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        ConvertTo-Json @{ app = if ($proc) { $proc.ProcessName } else { "" }; title = $titleBuilder.ToString(); x = $r.Left; y = $r.Top; width = ($r.Right - $r.Left); height = ($r.Bottom - $r.Top); active = $true; window_id = $h.ToInt64() } -Compress
'''
        try:
            value = json.loads(self._run_powershell_capture(script) or "{}")
            return self._normalize_window_record(value)
        except Exception:
            return None

    @staticmethod
    def _windows_send_key(key: str, modifiers: Any = None) -> str:
        raw = key.strip()
        normalized = raw.lower()
        parsed_modifiers: list[str] = []
        if isinstance(modifiers, str):
            parsed_modifiers.extend(part.strip().lower() for part in re.split(r"[+, ]+", modifiers) if part.strip())
        elif isinstance(modifiers, (list, tuple)):
            parsed_modifiers.extend(str(part).strip().lower() for part in modifiers if str(part).strip())
        if "+" in normalized:
            parts = [part for part in normalized.split("+") if part]
            parsed_modifiers.extend(part for part in parts[:-1] if part)
            normalized = parts[-1] if parts else normalized
            raw = normalized
        if normalized in {"back", "backward", "browserback", "browser_back"} and any(
            modifier in {"alt", "option"} for modifier in parsed_modifiers
        ):
            normalized = "left"
            raw = "left"
        key_map = {
            "enter": "{ENTER}",
            "return": "{ENTER}",
            "escape": "{ESC}",
            "esc": "{ESC}",
            "tab": "{TAB}",
            "back": "{BACKSPACE}",
            "bksp": "{BACKSPACE}",
            "bs": "{BACKSPACE}",
            "backspace": "{BACKSPACE}",
            "delete": "{DELETE}",
            "pageup": "{PGUP}",
            "pgup": "{PGUP}",
            "pagedown": "{PGDN}",
            "pgdn": "{PGDN}",
            "home": "{HOME}",
            "end": "{END}",
            "up": "{UP}",
            "down": "{DOWN}",
            "left": "{LEFT}",
            "right": "{RIGHT}",
            "space": " ",
            "plus": "{+}",
        }
        key_token = key_map.get(normalized)
        if key_token is None:
            key_token = raw if len(raw) == 1 else "{" + BrowserComputerController._windows_sendkeys_escape_token(raw.upper()) + "}"
        modifier_prefix = ""
        for modifier in parsed_modifiers:
            if modifier in {"ctrl", "control", "cmd", "command"}:
                modifier_prefix += "^"
            elif modifier in {"shift"}:
                modifier_prefix += "+"
            elif modifier in {"alt", "option"}:
                modifier_prefix += "%"
        return modifier_prefix + key_token

    @staticmethod
    def _windows_sendkeys_escape_token(value: str) -> str:
        return value.replace("{", "").replace("}", "")

    @staticmethod
    def _windows_sendkeys_escape_text(text: str) -> str:
        pieces: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\r":
                index += 1
                continue
            if char == "\n":
                pieces.append("{ENTER}")
            elif char == "\t":
                pieces.append("{TAB}")
            elif char == "{":
                pieces.append("{{}")
            elif char == "}":
                pieces.append("{}}")
            elif char in "+^%~()[]":
                pieces.append("{" + char + "}")
            else:
                pieces.append(char)
            index += 1
        return "".join(pieces)

    @staticmethod
    def _ps_single(value: str) -> str:
        return value.replace("'", "''")

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    @staticmethod
    def _run_powershell(script: str) -> None:
        executable = "powershell"
        try:
            subprocess.run([executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=True)
        except FileNotFoundError:
            subprocess.run(["pwsh", "-NoProfile", "-Command", script], check=True)

    @staticmethod
    def _run_powershell_capture(script: str) -> str:
        executable = "powershell" if shutil.which("powershell") else "pwsh"
        wrapped_script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script
        completed = subprocess.run(
            [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", wrapped_script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout or ""

    @staticmethod
    def _json_list(raw: str) -> list[Any]:
        if not raw.strip():
            return []
        value = json.loads(raw)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _capabilities() -> dict[str, bool]:
        system = platform.system()
        desktop_systems = {"Darwin", "Windows", "Linux"}
        return {
            "browser_open_url": True,
            "browser_persistent_profiles": True,
            "browser_cookie_management": True,
            "browser_cache_management": True,
            "screenshot": system in desktop_systems,
            "app_listing": system in desktop_systems,
            "app_selection": system in desktop_systems,
            "installed_app_listing": system in desktop_systems,
            "window_selection": system in desktop_systems,
            "desktop_actions": system in desktop_systems,
            "cursor_move": system in desktop_systems,
            "virtual_ai_cursor": True,
            "driver_auto_switch": system in desktop_systems,
            "visible_window_only": True,
            "platform_separated_drivers": True,
            "mac_swift_host": system == "Darwin",
            "linux_visible_driver": system == "Linux",
        }

    def _read_sessions(self) -> dict[str, Any]:
        try:
            value = json.loads(self._session_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _write_sessions(self, value: dict[str, Any]) -> None:
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _active_profile_id(self) -> str:
        sessions = self._read_sessions()
        return self._profile_id(sessions.get("active_profile_id") or "default")

    def _list_profiles(self) -> list[dict[str, Any]]:
        sessions = self._read_sessions()
        profiles = sessions.get("profiles") if isinstance(sessions.get("profiles"), dict) else {}
        self._ensure_profile("default", label="Default")
        sessions = self._read_sessions()
        profiles = sessions.get("profiles") if isinstance(sessions.get("profiles"), dict) else {}
        return [
            self._profile_summary(profile_id, record)
            for profile_id, record in sorted(profiles.items())
            if isinstance(record, dict)
        ]

    def _ensure_profile(self, profile_id: str, *, label: str | None = None) -> dict[str, Any]:
        profile_id = self._profile_id(profile_id)
        sessions = self._read_sessions()
        profiles = sessions.get("profiles") if isinstance(sessions.get("profiles"), dict) else {}
        now = self._now_iso()
        record = profiles.get(profile_id) if isinstance(profiles.get(profile_id), dict) else {}
        if not record:
            record = {"id": profile_id, "label": label or profile_id, "created_at": now}
        elif label:
            record["label"] = label
        record["profile_dir"] = str(self._profile_path(profile_id) / "browser-data")
        record["cache_dir"] = str(self._profile_path(profile_id) / "cache")
        record["cookie_jar"] = str(self._cookie_jar_path(profile_id))
        record["updated_at"] = now
        profiles[profile_id] = record
        sessions["profiles"] = profiles
        sessions.setdefault("active_profile_id", profile_id if profile_id != "default" else "default")
        sessions["updated_at"] = now
        self._write_sessions(sessions)
        (self._profile_path(profile_id) / "browser-data").mkdir(parents=True, exist_ok=True)
        (self._profile_path(profile_id) / "cache").mkdir(parents=True, exist_ok=True)
        return record

    def _profile_summary(self, profile_id: str, record: dict[str, Any]) -> dict[str, Any]:
        cookie_jar = self._read_cookie_jar(profile_id)
        cache_paths = [path for path in self._cache_paths(profile_id) if path.exists()]
        return {
            "id": profile_id,
            "label": record.get("label") or profile_id,
            "profile_dir": record.get("profile_dir") or str(self._profile_path(profile_id) / "browser-data"),
            "cache_dir": record.get("cache_dir") or str(self._profile_path(profile_id) / "cache"),
            "cookie_jar": str(self._cookie_jar_path(profile_id)),
            "managed_cookie_count": len(cookie_jar.get("cookies", [])),
            "cache_size_bytes": sum(self._path_size(path) for path in cache_paths),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }

    def _profile_path(self, profile_id: str) -> Path:
        return self._profile_root / self._profile_id(profile_id)

    @staticmethod
    def _profile_id(value: Any) -> str:
        raw = str(value or "default").strip().lower()
        cleaned = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-_")
        return (cleaned or "default")[:64]

    def _browser_launch_plan(self, url: str, profile_id: str, *, persistent: bool) -> dict[str, Any]:
        executable = self._find_browser_executable()
        profile_path = self._profile_path(profile_id)
        browser_data = profile_path / "browser-data"
        cache_dir = profile_path / "cache"
        if not persistent:
            return {"mode": "default_browser", "reason": "persistent=false"}
        if not executable:
            return {"mode": "default_browser", "reason": "no_supported_browser_found"}
        return {
            "mode": "managed_profile",
            "browser": str(executable),
            "profile_id": profile_id,
            "profile_dir": str(browser_data),
            "cache_dir": str(cache_dir),
            "command": [
                str(executable),
                f"--user-data-dir={browser_data}",
                f"--disk-cache-dir={cache_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--new-window",
                url,
            ],
        }

    def _find_browser_executable(self) -> Path | None:
        system = platform.system()
        candidates: list[Path] = []
        if system == "Darwin":
            candidates = [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
            ]
        elif system == "Windows":
            roots = [
                os.environ.get("LOCALAPPDATA"),
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
            ]
            for root in [Path(value) for value in roots if value]:
                candidates.extend(
                    [
                        root / "Google" / "Chrome" / "Application" / "chrome.exe",
                        root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    ]
                )
        else:
            for name in ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"]:
                resolved = shutil.which(name)
                if resolved:
                    candidates.append(Path(resolved))
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _cache_paths(self, profile_id: str) -> list[Path]:
        base = self._profile_path(profile_id)
        default_profile = base / "browser-data" / "Default"
        return [
            base / "cache",
            default_profile / "Cache",
            default_profile / "Code Cache",
            default_profile / "GPUCache",
            default_profile / "Service Worker" / "CacheStorage",
        ]

    def _browser_cookie_paths(self, profile_id: str) -> list[Path]:
        default_profile = self._profile_path(profile_id) / "browser-data" / "Default"
        return [
            default_profile / "Cookies",
            default_profile / "Cookies-journal",
            default_profile / "Network" / "Cookies",
            default_profile / "Network" / "Cookies-journal",
        ]

    def _cookie_jar_path(self, profile_id: str) -> Path:
        return self._profile_path(profile_id) / "managed_cookies.json"

    def _read_cookie_jar(self, profile_id: str) -> dict[str, Any]:
        try:
            value = json.loads(self._cookie_jar_path(profile_id).read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("cookies"), list):
                return value
        except Exception:
            pass
        return {"version": 1, "cookies": []}

    def _write_cookie_jar(self, profile_id: str, value: dict[str, Any]) -> None:
        self._profile_path(profile_id).mkdir(parents=True, exist_ok=True)
        self._cookie_jar_path(profile_id).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
        name = str(cookie.get("name") or "").strip()
        domain = str(cookie.get("domain") or cookie.get("url") or "").strip()
        if not name or not domain:
            raise ValueError("cookie.name and cookie.domain are required")
        return {
            "name": name,
            "value": str(cookie.get("value") or ""),
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "expires": cookie.get("expires"),
            "httpOnly": bool(cookie.get("httpOnly") or cookie.get("http_only")),
            "secure": bool(cookie.get("secure")),
            "sameSite": cookie.get("sameSite") or cookie.get("same_site"),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @staticmethod
    def _merge_cookies(current: list[Any], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for cookie in current:
            if not isinstance(cookie, dict):
                continue
            key = (str(cookie.get("domain") or ""), str(cookie.get("path") or "/"), str(cookie.get("name") or ""))
            if key[2]:
                merged[key] = cookie
        for cookie in incoming:
            merged[(cookie["domain"], cookie["path"], cookie["name"])] = cookie
        return list(merged.values())

    @staticmethod
    def _cookie_public_view(cookie: dict[str, Any], *, include_values: bool) -> dict[str, Any]:
        view = {key: value for key, value in cookie.items() if key != "value"}
        value = str(cookie.get("value") or "")
        view["value"] = value if include_values else ("***" if value else "")
        view["value_redacted"] = not include_values and bool(value)
        return view

    @staticmethod
    def _cookie_matches(cookie: dict[str, Any], *, name: str, domain: str, path: str) -> bool:
        if name and cookie.get("name") != name:
            return False
        if domain and cookie.get("domain") != domain:
            return False
        if path and cookie.get("path") != path:
            return False
        return bool(name or domain or path)

    def _path_size(self, path: Path) -> int:
        try:
            if path.is_file():
                return path.stat().st_size
            if not path.exists():
                return 0
            total = 0
            for item in path.rglob("*"):
                if item.is_file():
                    total += item.stat().st_size
            return total
        except Exception:
            return 0

    @staticmethod
    def _remove_path(path: Path) -> bool:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def _approval_required(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = _COMPUTER_APPROVAL_PROMPT
        response = {
            "action": action,
            "requires_approval": True,
            "approval_expires_in_seconds": 300,
            "approval_hint": "Approve the pending request in a trusted Rumi UI, then retry with the signed approval token.",
            "message": prompt,
            "user_prompt": prompt,
            "recovery": {
                "kind": "approval_required",
                "requires_approval": True,
                "prompt": prompt,
                "note": "foreground/on-screen operation is available after approval; approve the request or choose foreground work.",
                "recommended_next_actions": ["approve_request", "choose_foreground_work"],
            },
            "payload": payload,
        }
        warning = self._approval_warning(action, payload)
        if warning:
            response["approval_warning"] = warning
        return response

    @staticmethod
    def _approval_warning(action: str, payload: dict[str, Any]) -> str:
        if action == "computer.clipboard.read":
            if payload.get("include_content") is True:
                return (
                    "This approval returns the full system clipboard text to the model and tool result. "
                    "Do this only when the clipboard contents are safe to share."
                )
            return (
                "This approval reads the system clipboard and returns only a short preview by default. "
                "Full content requires include_content=true."
            )
        return ""

    def _consume_approval(self, payload: dict[str, Any], action: str, expected_payload: dict[str, Any]) -> bool:
        token = str(payload.get("approval_token") or "").strip()
        if not token:
            return False
        approval = self._approval_module()
        if approval is None:
            return self._consume_legacy_approval(token, action, expected_payload)
        expected_args = {"action": action, "payload": expected_payload}
        verification = approval.verify_execution_token(
            token,
            action,
            approval.hash_arguments(expected_args),
            pack_id="defaultspack",
        )
        if bool(getattr(verification, "valid", False)):
            return True
        return self._consume_legacy_approval(token, action, expected_payload)

    def _issue_legacy_approval(self, action: str, payload: dict[str, Any]) -> str:
        approvals = self._read_approvals()
        token = secrets.token_urlsafe(24)
        approvals[token] = {
            "action": action,
            "payload": payload,
            "expires_at": time.time() + 300,
        }
        self._write_approvals(approvals)
        return token

    def _consume_legacy_approval(self, token: str, action: str, expected_payload: dict[str, Any]) -> bool:
        approvals = self._read_approvals()
        record = approvals.pop(token, None)
        self._write_approvals(approvals)
        if not isinstance(record, dict):
            return False
        if record.get("action") != action:
            return False
        if record.get("payload") != expected_payload:
            return False
        if float(record.get("expires_at") or 0) < time.time():
            return False
        return True

    @staticmethod
    def _approval_module():
        try:
            from ecosystem.defaultspack.domain.safety import approval

            return approval
        except Exception:
            try:
                from domain.safety import approval

                return approval
            except Exception:
                return None

    def _read_approvals(self) -> dict[str, Any]:
        try:
            value = json.loads(self._approval_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
        except Exception:
            return {}
        now = time.time()
        return {
            token: record
            for token, record in value.items()
            if isinstance(record, dict) and float(record.get("expires_at") or 0) >= now
        }

    def _write_approvals(self, value: dict[str, Any]) -> None:
        self._approval_path.parent.mkdir(parents=True, exist_ok=True)
        self._approval_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _safe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key not in {"approved", "approval_token"}}
