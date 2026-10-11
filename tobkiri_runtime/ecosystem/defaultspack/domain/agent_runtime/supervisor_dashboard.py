"""Supervisor dashboard contract for runtime routing and sandbox visibility."""

from __future__ import annotations

from typing import Any, Iterable

from core_runtime.runtime_audit_helpers import redact_sensitive


STRUCTURED_OPERATION_LAYERS: list[dict[str, Any]] = [
    {
        "id": "shell",
        "label": "Shell",
        "kind": "structured",
        "priority": 10,
        "status": "preferred_when_allowed",
        "capabilities": ["command_exec", "test_runner", "logs"],
    },
    {
        "id": "filesystem",
        "label": "Filesystem",
        "kind": "structured",
        "priority": 20,
        "status": "preferred_when_allowed",
        "capabilities": ["read", "write", "diff"],
    },
    {
        "id": "playwright_cdp",
        "label": "Playwright / CDP",
        "kind": "structured",
        "priority": 30,
        "status": "preferred_for_browser",
        "capabilities": ["browser_dom", "accessibility_snapshot", "console_logs", "network_logs"],
    },
    {
        "id": "browser_logs",
        "label": "Browser logs",
        "kind": "structured",
        "priority": 40,
        "status": "preferred_for_debug",
        "capabilities": ["console", "network", "recording"],
    },
    {
        "id": "test_runner",
        "label": "Test runner",
        "kind": "structured",
        "priority": 50,
        "status": "preferred_for_regression",
        "capabilities": ["unit", "integration", "playwright_trace"],
    },
]

COMPUTER_FALLBACK_LAYERS: list[dict[str, Any]] = [
    {
        "id": "browser_cdp",
        "label": "Browser CDP",
        "kind": "visual_fallback",
        "priority": 80,
        "status": "use_after_structured_browser_api",
        "capabilities": ["browser_observe", "browser_action"],
    },
    {
        "id": "browser_companion",
        "label": "Browser companion",
        "kind": "visual_fallback",
        "priority": 90,
        "status": "use_for_extension_or_tab_bridge",
        "capabilities": ["extension_popup", "content_script", "tab_state"],
    },
    {
        "id": "computer_use",
        "label": "Computer use",
        "kind": "last_operation_layer",
        "priority": 100,
        "status": "fallback_only",
        "capabilities": ["screenshot", "click", "type", "scroll", "key"],
    },
]

SANDBOX_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "cloud",
        "label": "Cloud sandbox",
        "tier": "default",
        "default": True,
        "user_burden": "low",
        "install_required": False,
        "providers": ["browserbase", "e2b", "blaxel", "modal"],
        "capabilities": ["coding", "browser", "chrome_extension", "research"],
        "artifacts": ["screenshots", "video", "playwright_trace", "console_logs", "network_logs", "git_diff"],
    },
    {
        "id": "local_packaged",
        "label": "Packaged local sandbox",
        "tier": "pro_local",
        "default": False,
        "user_burden": "medium",
        "install_required": True,
        "providers": ["docker_sbx", "cua", "lima_containerd", "wsl2_containerd", "podman", "browser_only"],
        "capabilities": ["privacy_mode", "local_files", "desktop_gui", "microvm"],
        "artifacts": ["screenshots", "recording", "logs", "diffs"],
    },
    {
        "id": "byo_advanced",
        "label": "BYO advanced runtime",
        "tier": "advanced",
        "default": False,
        "user_burden": "high",
        "install_required": True,
        "providers": ["docker_desktop", "podman", "kasm", "vm"],
        "capabilities": ["enterprise_policy", "custom_network", "custom_images"],
        "artifacts": ["provider_defined"],
    },
]

RUNTIME_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "chrome-extension-eval",
        "runtime": "cloud_or_sbx",
        "targets": ["extension_dir", "test_site"],
        "capabilities": ["node", "chromium", "playwright", "chrome_extension", "xvfb"],
        "artifacts": [
            "popup_screenshot",
            "service_worker_logs",
            "console_logs",
            "network_logs",
            "playwright_trace",
            "generated_test",
        ],
        "fallback": "computer_use_gui",
        "risk": "medium",
    },
]

EVENT_SCHEMA: list[dict[str, str]] = [
    {"type": "run.started", "description": "Run lifecycle start."},
    {"type": "agent.step", "description": "Agent planner or execution step."},
    {"type": "tool.call", "description": "Tool invocation input."},
    {"type": "tool.result", "description": "Tool invocation result."},
    {"type": "computer.observe", "description": "Screenshot or accessibility observation."},
    {"type": "computer.action", "description": "Mouse, keyboard, scroll, or key action."},
    {"type": "browser.console", "description": "Browser console log line."},
    {"type": "browser.network", "description": "Browser network event."},
    {"type": "shell.exec", "description": "Shell command execution."},
    {"type": "file.diff", "description": "File diff artifact."},
    {"type": "approval.required", "description": "Human approval gate."},
    {"type": "risk.alert", "description": "Prompt injection or policy risk signal."},
    {"type": "run.finished", "description": "Run lifecycle terminal state."},
]

SUPERVISOR_CAPABILITY_FLAGS = {
    "snapshot": True,
    "live_screen": False,
    "takeover": False,
    "replay": False,
}

# Passive snapshot affordances only. Live controls need real endpoints before
# they can be advertised by this contract.
ACTION_BUTTONS = [
    "inspect_snapshot",
    "view_diff",
    "export_artifact",
]

SECURITY_GUARDRAILS = [
    "real_computer_is_opt_in",
    "secrets_are_proxy_injected",
    "network_allowlist_by_default",
    "clipboard_download_upload_monitored",
    "external_side_effects_require_approval",
    "sandbox_destroyed_at_session_end",
    "snapshot_evidence_is_recorded",
]

STORAGE_TARGETS = {
    "metadata": "postgres",
    "artifacts": "object_storage",
    "event_logs": "jsonl_or_clickhouse",
    "traces": "opentelemetry",
    "llm_trace": "langfuse_optional",
}

EVENT_PAYLOAD_ALLOWED_KEYS = {
    "action",
    "agent_id",
    "artifact_id",
    "code",
    "exit_code",
    "path",
    "reason",
    "risk",
    "risk_level",
    "status",
    "tool",
    "tool_name",
}
MAX_EVENT_PAYLOAD_FIELDS = 8
MAX_EVENT_PAYLOAD_VALUE_CHARS = 160


def build_supervisor_dashboard_snapshot(
    *,
    run_store: Any | None = None,
    stale_after_seconds: int = 600,
    event_limit: int = 12,
) -> dict[str, Any]:
    """Return a dashboard-safe snapshot of routing, sandbox, and run state."""

    capabilities = dict(SUPERVISOR_CAPABILITY_FLAGS)
    metrics, sessions, selected_session, recent_events = _runtime_metrics(
        run_store=run_store,
        stale_after_seconds=stale_after_seconds,
        event_limit=event_limit,
        capabilities=capabilities,
    )
    return {
        "capabilities": capabilities,
        "router": build_runtime_router_contract(),
        "sandbox_providers": [dict(provider) for provider in SANDBOX_PROVIDERS],
        "runtime_templates": [dict(template) for template in RUNTIME_TEMPLATES],
        "metrics": metrics,
        "sessions": sessions,
        "selected_session": selected_session,
        "recent_events": recent_events,
        "event_schema": [dict(item) for item in EVENT_SCHEMA],
        "storage_targets": dict(STORAGE_TARGETS),
        "action_buttons": list(ACTION_BUTTONS),
        "security_guardrails": list(SECURITY_GUARDRAILS),
    }


def build_runtime_router_contract() -> dict[str, Any]:
    """Describe the preferred routing policy without executing any action."""

    operation_layers = [dict(layer) for layer in STRUCTURED_OPERATION_LAYERS]
    fallback_layers = [dict(layer) for layer in COMPUTER_FALLBACK_LAYERS]
    return {
        "policy": "structured_first_computer_last",
        "structured_first": True,
        "computer_use_role": "last_operation_layer",
        "preferred_order": [layer["id"] for layer in operation_layers],
        "fallback_order": [layer["id"] for layer in fallback_layers],
        "operation_layers": operation_layers,
        "fallback_layers": fallback_layers,
        "computer_driver_order": _computer_driver_order(),
    }


def _runtime_metrics(
    *,
    run_store: Any | None,
    stale_after_seconds: int,
    event_limit: int,
    capabilities: dict[str, bool],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    store = run_store if run_store is not None else _default_agent_run_store()
    base_metrics: dict[str, Any] = {
        "available": store is not None,
        "active_runs": 0,
        "waiting_approvals": 0,
        "stale_runs": 0,
        "failed_runs": 0,
        "screen_sessions": 0,
        "replay_ready": 0,
        "artifact_streams": ["screenshots", "traces", "diffs", "logs"],
    }
    if store is None:
        return base_metrics, [], None, []

    try:
        active_runs = _safe_call(store, "list_active", limit=100)
        waiting_runs = _safe_call(store, "list_waiting_approval", limit=100)
        stale_runs = _safe_call(store, "list_stale", stale_after_seconds=stale_after_seconds, limit=100)
        failed_runs = _list_failed_runs(store)
    except Exception:
        base_metrics["available"] = False
        return base_metrics, [], None, []

    sessions = _session_grid(
        _unique_runs([*waiting_runs, *stale_runs, *active_runs, *failed_runs]),
        capabilities=capabilities,
    )
    selected_session = sessions[0] if sessions else None
    recent_events = _recent_events(store, sessions, limit=event_limit)
    metrics = {
        **base_metrics,
        "active_runs": len(active_runs),
        "waiting_approvals": len(waiting_runs),
        "stale_runs": len(stale_runs),
        "failed_runs": len(failed_runs),
        "screen_sessions": (
            sum(1 for session in sessions if session.get("screen", {}).get("available"))
            if capabilities.get("live_screen")
            else 0
        ),
        "replay_ready": (
            sum(1 for session in sessions if session.get("replay", {}).get("available"))
            if capabilities.get("replay")
            else 0
        ),
    }
    return metrics, sessions, selected_session, recent_events


def _default_agent_run_store() -> Any | None:
    try:
        from .run_store import AgentRunStore

        return AgentRunStore()
    except Exception:
        return None


def _safe_call(store: Any, method_name: str, **kwargs: Any) -> list[dict[str, Any]]:
    method = getattr(store, method_name, None)
    if not callable(method):
        return []
    value = method(**kwargs)
    return [dict(item) for item in value or [] if isinstance(item, dict)]


def _list_failed_runs(store: Any) -> list[dict[str, Any]]:
    failed = _safe_call(store, "list_runs", status="failed", limit=50)
    error = _safe_call(store, "list_runs", status="error", limit=50)
    return _unique_runs([*failed, *error])


def _unique_runs(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in runs:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        result.append(run)
    return result


def _session_grid(runs: list[dict[str, Any]], *, capabilities: dict[str, bool]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for run in runs[:12]:
        execution = _string_object_dict(run.get("execution_json"))
        artifacts = _string_object_dict(execution.get("artifacts"))
        screen = _string_object_dict(execution.get("screen"))
        replay = _string_object_dict(execution.get("replay"))
        live_screen_available = bool(
            capabilities.get("live_screen")
            and (screen.get("available") or screen.get("url") or screen.get("stream_url"))
        )
        replay_available = bool(
            capabilities.get("replay")
            and (replay.get("available") or replay.get("url") or replay.get("recording_url"))
        )
        sessions.append(
            {
                "run_id": run.get("run_id"),
                "agent_id": run.get("agent_id"),
                "task": _truncate(str(run.get("task") or ""), 120),
                "status": run.get("status"),
                "updated_at": run.get("updated_at"),
                "heartbeat_at": run.get("heartbeat_at"),
                "risk": _runtime_risk(run),
                "screen": {
                    "available": live_screen_available,
                    "provider": screen.get("provider") or _provider_from_runtime(run),
                    "url": (screen.get("url") or screen.get("stream_url")) if live_screen_available else None,
                    "screenshot_url": screen.get("screenshot_url"),
                },
                "replay": {
                    "available": replay_available,
                    "url": (replay.get("url") or replay.get("recording_url")) if replay_available else None,
                },
                "artifacts": {
                    "screenshots": _artifact_count(artifacts, "screenshots"),
                    "logs": _artifact_count(artifacts, "logs"),
                    "diffs": _artifact_count(artifacts, "diffs"),
                    "traces": _artifact_count(artifacts, "traces"),
                },
            }
        )
    return sessions


def _runtime_risk(run: dict[str, Any]) -> str:
    runtime_profile = _string_object_dict(run.get("runtime_profile_json"))
    policy = _string_object_dict(runtime_profile.get("policy"))
    value = str(policy.get("risk") or policy.get("risk_level") or "").strip().lower()
    if value:
        return value
    status = str(run.get("status") or "")
    return "high" if status == "waiting_approval" else "medium" if status in {"running", "stale"} else "low"


def _provider_from_runtime(run: dict[str, Any]) -> str | None:
    runtime_profile = _string_object_dict(run.get("runtime_profile_json"))
    sandbox = _string_object_dict(runtime_profile.get("sandbox"))
    provider = str(sandbox.get("provider") or runtime_profile.get("sandbox_provider") or "").strip()
    return provider or None


def _string_object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str)
    }


def _artifact_count(artifacts: dict[str, object], key: str) -> int:
    value = artifacts.get(key)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if not isinstance(value, (str, int, float)):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _recent_events(store: Any, sessions: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    events_method = getattr(store, "events", None)
    if not callable(events_method):
        return []
    events: list[dict[str, Any]] = []
    for session in sessions:
        run_id = str(session.get("run_id") or "")
        if not run_id:
            continue
        try:
            rows = events_method(run_id, limit=4)
        except Exception:
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            events.append(
                {
                    "run_id": run_id,
                    "event_type": row.get("event_type"),
                    "created_at": row.get("created_at"),
                    "payload": _dashboard_event_payload(row.get("payload_json")),
                }
            )
    events.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return events[: max(0, int(limit))]


def _computer_driver_order() -> dict[str, list[str]]:
    """Return the finite driver order advertised by the v4 host catalog."""

    return {
        "darwin": [
            "browser_cdp",
            "browser_companion",
            "mac_accessibility",
            "mac_apple_events",
            "mac_cgevent_pid",
            "mac_screen_capture",
            "mac_foreground",
        ],
        "win32": [
            "browser_cdp",
            "browser_companion",
            "windows_uia",
            "windows_postmessage",
            "windows_foreground",
            "local_visible",
        ],
    }


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[: max(0, max_length)]
    return value[: max(0, max_length - 3)].rstrip() + "..."


def _dashboard_event_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    redacted = redact_sensitive(payload)
    allowed_items: list[tuple[str, Any]] = []
    for key, value in redacted.items():
        key_text = str(key)
        if key_text not in EVENT_PAYLOAD_ALLOWED_KEYS:
            continue
        allowed_items.append((key_text, _dashboard_payload_value(value)))
        if len(allowed_items) >= MAX_EVENT_PAYLOAD_FIELDS:
            break
    result = {key: value for key, value in allowed_items}
    omitted = max(0, len(redacted) - len(result))
    if omitted:
        result["_omitted_fields"] = omitted
    return result


def _dashboard_payload_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate(value, MAX_EVENT_PAYLOAD_VALUE_CHARS)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            "type": "object",
            "field_count": len(value),
            "keys": sorted(str(key) for key in value.keys())[:MAX_EVENT_PAYLOAD_FIELDS],
        }
    if isinstance(value, (list, tuple, set)):
        return {"type": "list", "count": len(value)}
    return _truncate(str(value), MAX_EVENT_PAYLOAD_VALUE_CHARS)
