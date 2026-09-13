from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core_runtime.profile_workspace import ProfileWorkspaceManager, validate_profile_id


class AiInputTraceStore:
    def __init__(self, workspace_manager: ProfileWorkspaceManager | None = None) -> None:
        self.workspace_manager = workspace_manager or ProfileWorkspaceManager()

    def trace_dir(self, profile_id: str) -> Path:
        paths = self.workspace_manager.paths_for_profile(validate_profile_id(profile_id))
        return paths.root / "runtime_traces"

    def save_trace(self, profile_id: str, trace: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(trace.get("trace_id") or "").strip() or f"ait_{int(time.time())}"
        payload = {
            "trace_id": trace_id,
            "created_at": int(time.time()),
            **dict(trace),
        }
        target_dir = self.trace_dir(profile_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        trace_path = target_dir / f"{trace_id}.json"
        latest_path = target_dir / "latest_ai_input.json"
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        trace_path.write_text(rendered, encoding="utf-8")
        latest_path.write_text(rendered, encoding="utf-8")
        return payload

    def append_blocked_event(self, profile_id: str, event: dict[str, Any]) -> dict[str, Any]:
        target_dir = self.trace_dir(profile_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        latest_path = target_dir / "latest_ai_input.json"
        payload: dict[str, Any]
        if latest_path.is_file():
            try:
                raw = json.loads(latest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            payload = raw if isinstance(raw, dict) else {}
        else:
            payload = {}

        trace_id = str(payload.get("trace_id") or "").strip() or f"ait_blocked_{int(time.time())}"
        payload.setdefault("trace_id", trace_id)
        payload.setdefault("profile_id", profile_id)
        payload.setdefault("created_at", int(time.time()))
        blocked = payload.get("blocked")
        if not isinstance(blocked, list):
            blocked = []
            payload["blocked"] = blocked
        blocked_event = {
            "created_at": int(time.time()),
            **dict(event),
        }
        blocked.append(blocked_event)

        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (target_dir / f"{trace_id}.json").write_text(rendered, encoding="utf-8")
        latest_path.write_text(rendered, encoding="utf-8")
        return payload

    def list_traces(self, profile_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        target_dir = self.trace_dir(profile_id)
        if not target_dir.is_dir():
            return []
        traces: list[dict[str, Any]] = []
        for path in sorted(target_dir.glob("ait_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                traces.append(_trace_summary(raw))
            if len(traces) >= limit:
                break
        return traces

    def get_trace(self, profile_id: str, trace_id: str) -> dict[str, Any] | None:
        safe_trace_id = str(trace_id or "").strip()
        if not safe_trace_id or "/" in safe_trace_id or "\\" in safe_trace_id:
            return None
        path = self.trace_dir(profile_id) / f"{safe_trace_id}.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None


def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    token_estimate = trace.get("token_estimate") if isinstance(trace.get("token_estimate"), dict) else {}
    provider_summary = (
        trace.get("provider_payload_summary")
        if isinstance(trace.get("provider_payload_summary"), dict)
        else {}
    )
    return {
        "trace_id": trace.get("trace_id"),
        "created_at": trace.get("created_at"),
        "conversation_id": trace.get("conversation_id"),
        "run_id": trace.get("run_id"),
        "profile_id": trace.get("profile_id"),
        "token_estimate": token_estimate,
        "provider_payload_summary": provider_summary,
        "blocked_count": len(blocked) if isinstance((blocked := trace.get("blocked")), list) else 0,
    }
