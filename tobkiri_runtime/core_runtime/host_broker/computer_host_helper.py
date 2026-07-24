from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _ensure_import_path() -> None:
    base = Path(__file__).resolve().parents[2]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def main() -> int:
    _ensure_import_path()
    os.environ["RUMI_COMPUTER_HOST_INTERNAL"] = "1"

    request = json.loads(sys.stdin.read() or "{}")
    action = str(request.get("function_id") or "").strip()
    payload = dict(request.get("args") or {})
    viewer_host_approved = bool(request.get("viewer_host_approved"))

    try:
        if action.startswith("browser."):
            from ecosystem.rumi_browser_host_service_pack.runtime.runner import (
                run_browser_host_action,
            )

            artifact_root = _validated_artifact_root(request.get("artifact_root"))
            result = run_browser_host_action(
                action,
                payload,
                viewer_host_approved=viewer_host_approved,
                artifact_root=artifact_root,
            )
        elif action.startswith("computer.clipboard."):
            from ecosystem.rumi_clipboard_host_service_pack.runtime.runner import (
                run_clipboard_host_action,
            )

            result = run_clipboard_host_action(
                action,
                payload,
                viewer_host_approved=viewer_host_approved,
            )
        else:
            from ecosystem.rumi_default_tools_pack.domain.computer import (
                create_default_computer_tool_service,
            )

            _validated_artifact_root(request.get("artifact_root"))
            result = _run_desktop_action(
                create_default_computer_tool_service(),
                action,
                payload,
                viewer_host_approved=viewer_host_approved,
            )
    except ValueError as exc:
        print(
            json.dumps(
                {"ok": False, "error_code": "INVALID_ARTIFACT_ROOT", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:  # pragma: no cover - caller converts to broker error
        print(json.dumps({"ok": False, "error_code": "VIEWER_HOST_FAILED", "error": str(exc)}, ensure_ascii=False))
        return 0

    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


def _run_desktop_action(
    service: Any,
    action: str,
    payload: dict[str, Any],
    *,
    viewer_host_approved: bool,
) -> dict[str, Any]:
    if not viewer_host_approved:
        raise PermissionError("Viewer approval is required")
    target = {
        key: payload.get(key)
        for key in (
            "surface_id",
            "observation_revision",
            "coordinate_space",
            "app",
            "application",
            "pid",
            "window_id",
            "window_title",
            "title",
        )
        if payload.get(key) not in (None, "")
    }
    if action == "computer.doctor":
        return dict(service.doctor())
    if action in {
        "computer.observe",
        "computer.screenshot",
        "computer.ocr",
        "computer.ax_tree",
        "computer.context",
    }:
        result = dict(service.observe(target))
        result.setdefault("action", action)
        return result
    if action in {"computer.apps", "computer.windows"}:
        surfaces = list(service.list_surfaces())
        return {"action": action, "surfaces": surfaces}
    if action == "computer.move":
        return dict(service.move(target, _int(payload, "x"), _int(payload, "y")))
    if action == "computer.click":
        return dict(
            service.click(
                target,
                _int(payload, "x"),
                _int(payload, "y"),
                str(payload.get("button") or "left"),
            )
        )
    if action == "computer.drag":
        return dict(
            service.drag(
                target,
                _int(payload, "x1", "from_x"),
                _int(payload, "y1", "from_y"),
                _int(payload, "x2", "to_x"),
                _int(payload, "y2", "to_y"),
            )
        )
    if action == "computer.type":
        text = str(payload.get("text") or "")
        if not text:
            raise ValueError("computer.type requires text")
        return dict(service.type_text(target, text))
    if action == "computer.key":
        key = str(payload.get("key_combo") or payload.get("key") or "")
        if not key:
            raise ValueError("computer.key requires a key")
        return dict(service.key(target, key))
    if action == "computer.scroll":
        return dict(
            service.scroll(
                target,
                _int(payload, "x"),
                _int(payload, "y"),
                str(payload.get("direction") or "down"),
                max(1, min(100, _int(payload, "clicks", "amount", default=3))),
            )
        )
    if action in {
        "computer.select_app",
        "computer.show_app",
        "computer.select_window",
        "computer.click_text",
        "computer.semantic_action",
    }:
        intent = str(payload.get("intent") or action.removeprefix("computer."))
        return dict(service.semantic_action(target, intent, dict(payload)))
    if action == "computer.pid_event":
        intent = str(payload.get("intent") or payload.get("action_type") or "")
        return dict(service.pid_event(intent, target, dict(payload)))
    return {
        "action": action,
        "is_error": True,
        "error_type": "desktop_runner_unavailable",
    }


def _int(
    payload: dict[str, Any],
    *keys: str,
    default: int = 0,
) -> int:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        return int(value)
    return default


def _validated_artifact_root(raw_value: object) -> Path | None:
    if raw_value is None:
        return None
    value = str(raw_value or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser().resolve()
    if candidate.name != "computer":
        raise ValueError("artifact_root must end with tools/computer.")
    tools_dir = candidate.parent
    workspace_dir = tools_dir.parent
    conversation_dir = workspace_dir.parent
    if tools_dir.name != "tools" or workspace_dir.name != "workspace" or not conversation_dir.name:
        raise ValueError("artifact_root must be inside a conversation workspace tools/computer directory.")
    for root in _allowed_conversation_roots():
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) == 4 and relative.parts[1:] == ("workspace", "tools", "computer"):
            return candidate
    raise ValueError("artifact_root is outside the allowed conversation workspace roots.")


def _allowed_conversation_roots() -> list[Path]:
    roots: list[Path] = []
    override = str(os.environ.get("RUMI_DEFAULTSPACK_CHAT_STORE_PATH") or "").strip()
    if override:
        roots.append(Path(override).expanduser().resolve().parent / "conversations")
    base = Path(__file__).resolve().parents[2]
    roots.append(base / "ecosystem" / "defaultspack" / "user_data" / "shared" / "chat" / "conversations")

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = root.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(resolved)
    return deduped


if __name__ == "__main__":
    raise SystemExit(main())
