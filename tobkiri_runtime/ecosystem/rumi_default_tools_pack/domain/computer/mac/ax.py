"""Accessibility API helpers for macOS.

Wraps pyobjc ApplicationServices to read the AX tree, find elements,
press buttons, set values, and raise windows. All public functions fail
closed when pyobjc is unavailable or the platform is not macOS.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import sys
import time
from typing import Any

_AX_AVAILABLE = False

if sys.platform == "darwin":
    try:
        from ApplicationServices import (  # type: ignore[import]
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            AXUIElementCopyActionNames,
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementCreateSystemWide,
            AXUIElementPerformAction,
            AXUIElementSetAttributeValue,
        )
        from CoreFoundation import (  # type: ignore[import]
            kCFBooleanTrue,
        )

        _AX_AVAILABLE = True
    except ImportError:
        pass


_ELEMENT_TTL_SECONDS = 10.0
_MAX_CHILDREN = 75
_MAX_DEPTH = 6
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "button",
    "click",
    "control",
    "field",
    "item",
    "menu",
    "on",
    "open",
    "press",
    "select",
    "set",
    "the",
    "to",
    "type",
}


@dataclass
class _StoredElement:
    ref: Any
    expires_at: float
    snapshot: dict[str, Any]


_ELEMENT_STORE: dict[str, _StoredElement] = {}


def ax_import_available() -> bool:
    """Return whether the native pyobjc AX symbols imported successfully."""
    return bool(_AX_AVAILABLE and sys.platform == "darwin")


def ax_is_trusted() -> bool:
    """Check if the current process has Accessibility permission."""
    if not _native_ax_ready():
        return False
    try:
        return bool(AXIsProcessTrusted())  # type: ignore[name-defined]
    except Exception:
        return False


def ax_prompt_permission() -> bool:
    """Prompt the user for Accessibility permission if not granted."""
    if not _native_ax_ready():
        return False
    try:
        options = {b"AXTrustedCheckOptionPrompt": kCFBooleanTrue}  # type: ignore[name-defined]
        return bool(AXIsProcessTrustedWithOptions(options))  # type: ignore[name-defined]
    except Exception:
        return False


def ax_list_windows(pid: int | None) -> list[dict]:
    """List windows for a given PID via the AX API."""
    if not _native_ax_ready() or pid is None:
        return []
    try:
        app_ref = AXUIElementCreateApplication(pid)  # type: ignore[name-defined]
        windows = _as_list(_get_attr(app_ref, "AXWindows"))
        result = []
        for index, window in enumerate(windows[:_MAX_CHILDREN]):
            element_id = _stable_element_id(pid, f"windows/{index}", window)
            node = _element_snapshot(window, element_id)
            node["window_id"] = _coerce_int(_get_attr(window, "AXWindowNumber"))
            _store_element(element_id, window, node)
            result.append(node)
        return result
    except Exception:
        return []


def ax_get_tree(
    pid: int | None = None,
    app: str | None = None,
    window_title: str | None = None,
    window_id: int | None = None,
) -> dict:
    """Get the accessibility tree for a target application/window."""
    if not _native_ax_ready():
        return {}
    if pid is None and app is None:
        return {}
    try:
        pid = pid if pid is not None else _resolve_pid(app)
        if pid is None:
            return {}
        app_ref = AXUIElementCreateApplication(pid)  # type: ignore[name-defined]
        root = _select_root(app_ref, window_title=window_title, window_id=window_id)
        return _build_tree(root, depth=0, max_depth=_MAX_DEPTH, pid=pid, path="root")
    except Exception:
        return {}


def ax_find_candidates(
    pid: int | None = None,
    app: str | None = None,
    role: str | None = None,
    title: str | None = None,
    description: str | None = None,
    point: tuple[int, int] | None = None,
    intent: str | None = None,
    window_title: str | None = None,
    window_id: int | None = None,
) -> list[dict]:
    """Find and score AX elements matching the given criteria."""
    if not _native_ax_ready():
        return []
    try:
        pid = pid if pid is not None else _resolve_pid(app)
        if pid is None:
            return []
        app_ref = AXUIElementCreateApplication(pid)  # type: ignore[name-defined]
        root = _select_root(app_ref, window_title=window_title, window_id=window_id)
        candidates = _collect_elements(root, depth=0, max_depth=_MAX_DEPTH, pid=pid, path="root")
        results = []
        for element in candidates:
            if role and _norm(element.get("role")) != _norm(role):
                continue
            if title and _norm(title) not in _norm(element.get("title")):
                continue
            if description and _norm(description) not in _norm(element.get("description")):
                continue
            score = _score_candidate(element, point=point, intent=intent)
            if point is not None and not _contains_point(element.get("frame"), point):
                continue
            if intent and score <= 0:
                continue
            scored = dict(element)
            scored["score"] = round(score, 3)
            results.append(scored)
        results.sort(key=lambda item: item.get("score", 0), reverse=True)
        return results
    except Exception:
        return []


def ax_press(element_id: str) -> bool:
    """Press (invoke AXPress) on an element by its id."""
    if not _native_ax_ready():
        return False
    element = _resolve_element(element_id)
    if element is None:
        return False
    return _perform_action(element, "AXPress")


def ax_set_value(
    pid: int | None,
    app: str | None,
    value: str,
    element_id: str | None = None,
) -> bool:
    """Set the value of a focused or specified AX element."""
    if not _native_ax_ready():
        return False
    try:
        element = _resolve_element(element_id) if element_id else None
        if element is None:
            pid = pid if pid is not None else _resolve_pid(app)
            if pid is None:
                return False
            app_ref = AXUIElementCreateApplication(pid)  # type: ignore[name-defined]
            element = _get_attr(app_ref, "AXFocusedUIElement")
        if element is None:
            return False
        return _set_attr(element, "AXValue", value)
    except Exception:
        return False


def ax_raise(window_id: int | str | None = None) -> bool:
    """Raise (bring to front) a window by AX element id or cached window id."""
    if not _native_ax_ready():
        return False
    try:
        element = None
        if isinstance(window_id, str):
            element = _resolve_element(window_id)
        elif isinstance(window_id, int):
            element = _find_stored_window(window_id)
        else:
            system_ref = AXUIElementCreateSystemWide()  # type: ignore[name-defined]
            element = _get_attr(system_ref, "AXFocusedWindow")
        if element is None:
            return False
        return _perform_action(element, "AXRaise")
    except Exception:
        return False


# --- internal helpers ---


def _native_ax_ready() -> bool:
    return sys.platform == "darwin" and _AX_AVAILABLE


def _resolve_pid(app: str | None) -> int | None:
    """Resolve an app name to a PID."""
    if not app:
        return None
    try:
        from AppKit import NSWorkspace  # type: ignore[import]

        for running in NSWorkspace.sharedWorkspace().runningApplications():
            if app.lower() in (running.localizedName() or "").lower():
                return int(running.processIdentifier())
    except Exception:
        pass
    return None


def _select_root(
    app_ref: Any,
    *,
    window_title: str | None,
    window_id: int | None,
) -> Any:
    if not window_title and window_id is None:
        return app_ref
    for window in _as_list(_get_attr(app_ref, "AXWindows")):
        if window_id is not None:
            candidate_id = _coerce_int(_get_attr(window, "AXWindowNumber"))
            if candidate_id == window_id:
                return window
        if window_title:
            title = str(_get_attr(window, "AXTitle") or "")
            if window_title.lower() in title.lower():
                return window
    return app_ref


def _build_tree(element: Any, depth: int, max_depth: int, pid: int, path: str) -> dict:
    """Recursively build a dict representation of the AX tree."""
    if depth > max_depth:
        return {}
    try:
        element_id = _stable_element_id(pid, path, element)
        node = _element_snapshot(element, element_id)
        _store_element(element_id, element, node)
        children = _child_elements(element)
        if children and depth < max_depth:
            node["children"] = [
                child_node
                for index, child in enumerate(children[:_MAX_CHILDREN])
                if (child_node := _build_tree(child, depth + 1, max_depth, pid, f"{path}/{index}"))
            ]
        return node
    except Exception:
        return {}


def _collect_elements(element: Any, depth: int, max_depth: int, pid: int, path: str) -> list[dict]:
    """Flatten the AX tree into a list of element dicts."""
    if depth > max_depth:
        return []
    results: list[dict] = []
    try:
        element_id = _stable_element_id(pid, path, element)
        node = _element_snapshot(element, element_id)
        _store_element(element_id, element, node)
        results.append(node)
        if depth < max_depth:
            for index, child in enumerate(_child_elements(element)[:_MAX_CHILDREN]):
                results.extend(_collect_elements(child, depth + 1, max_depth, pid, f"{path}/{index}"))
    except Exception:
        pass
    return results


def _element_snapshot(element: Any, element_id: str) -> dict[str, Any]:
    role = str(_get_attr(element, "AXRole") or "")
    title = str(_get_attr(element, "AXTitle") or "")
    desc = str(_get_attr(element, "AXDescription") or "")
    window_number = _coerce_int(_get_attr(element, "AXWindowNumber"))
    node: dict[str, Any] = {
        "id": element_id,
        "role": role,
        "title": title,
        "description": desc,
        "value": _safe_value(_get_attr(element, "AXValue")),
        "enabled": _safe_enabled(_get_attr(element, "AXEnabled")),
        "frame": _frame(element),
        "actions": _action_names(element),
    }
    if window_number is not None:
        node["window_id"] = window_number
    return node


def _stable_element_id(pid: int, path: str, element: Any) -> str:
    role = str(_get_attr(element, "AXRole") or "")
    title = str(_get_attr(element, "AXTitle") or "")
    desc = str(_get_attr(element, "AXDescription") or "")
    digest = hashlib.sha1(f"{path}|{role}|{title}|{desc}".encode("utf-8")).hexdigest()[:16]
    return f"ax:{pid}:{digest}"


def _store_element(element_id: str, element: Any, snapshot: dict[str, Any]) -> None:
    _prune_store()
    _ELEMENT_STORE[element_id] = _StoredElement(
        ref=element,
        expires_at=time.monotonic() + _ELEMENT_TTL_SECONDS,
        snapshot=dict(snapshot),
    )


def _resolve_element(element_id: str | None) -> Any | None:
    if not element_id or not str(element_id).startswith("ax:"):
        return None
    _prune_store()
    stored = _ELEMENT_STORE.get(str(element_id))
    if stored is None:
        return None
    return stored.ref


def _find_stored_window(window_id: int) -> Any | None:
    _prune_store()
    for stored in _ELEMENT_STORE.values():
        if stored.snapshot.get("window_id") == window_id:
            return stored.ref
    return None


def _prune_store() -> None:
    now = time.monotonic()
    expired = [element_id for element_id, stored in _ELEMENT_STORE.items() if stored.expires_at <= now]
    for element_id in expired:
        _ELEMENT_STORE.pop(element_id, None)


def _get_attr(element: Any, attr: str) -> Any:
    try:
        method = getattr(element, "copyAttributeValue_", None)
        if callable(method):
            return _unwrap_ax_result(method(attr))
        return _unwrap_ax_result(AXUIElementCopyAttributeValue(element, attr, None))  # type: ignore[name-defined]
    except Exception:
        return None


def _set_attr(element: Any, attr: str, value: Any) -> bool:
    try:
        method = getattr(element, "setAttributeValue_value_", None)
        if callable(method):
            return _ax_success(method(attr, value))
        return _ax_success(AXUIElementSetAttributeValue(element, attr, value))  # type: ignore[name-defined]
    except Exception:
        return False


def _perform_action(element: Any, action: str) -> bool:
    try:
        method = getattr(element, "performAction_", None)
        if callable(method):
            return _ax_success(method(action))
        return _ax_success(AXUIElementPerformAction(element, action))  # type: ignore[name-defined]
    except Exception:
        return False


def _action_names(element: Any) -> list[str]:
    try:
        method = getattr(element, "actionNames", None)
        if callable(method):
            return [str(action) for action in _as_list(method())]
        return [str(action) for action in _as_list(_unwrap_ax_result(AXUIElementCopyActionNames(element, None)))]  # type: ignore[name-defined]
    except Exception:
        return []


def _unwrap_ax_result(result: Any) -> Any:
    if isinstance(result, tuple):
        if len(result) >= 2:
            err, value = result[0], result[1]
            return value if _ax_success(err) else None
        if len(result) == 1:
            return result[0]
    return result


def _ax_success(result: Any) -> bool:
    if isinstance(result, tuple):
        return bool(result) and _ax_success(result[0])
    if result is None:
        return True
    try:
        return int(result) == 0
    except (TypeError, ValueError):
        return result is True


def _child_elements(element: Any) -> list[Any]:
    children = _as_list(_get_attr(element, "AXChildren"))
    windows = _as_list(_get_attr(element, "AXWindows"))
    if not windows:
        return children
    seen: set[int] = set()
    merged = []
    for child in windows + children:
        marker = id(child)
        if marker not in seen:
            seen.add(marker)
            merged.append(child)
    return merged


def _frame(element: Any) -> dict[str, float]:
    rect = _get_attr(element, "AXFrame")
    frame = _rect_to_frame(rect)
    if frame:
        return frame
    position = _point_to_pair(_get_attr(element, "AXPosition"))
    size = _size_to_pair(_get_attr(element, "AXSize"))
    if position and size:
        return {
            "x": position[0],
            "y": position[1],
            "width": size[0],
            "height": size[1],
        }
    return {}


def _rect_to_frame(value: Any) -> dict[str, float]:
    if value is None:
        return {}
    origin = getattr(value, "origin", None)
    size = getattr(value, "size", None)
    if origin is not None and size is not None:
        point = _point_to_pair(origin)
        dims = _size_to_pair(size)
        if point and dims:
            return {"x": point[0], "y": point[1], "width": dims[0], "height": dims[1]}
    if isinstance(value, dict):
        try:
            return {
                "x": float(value.get("x", value.get("X", 0))),
                "y": float(value.get("y", value.get("Y", 0))),
                "width": float(value.get("width", value.get("Width", 0))),
                "height": float(value.get("height", value.get("Height", 0))),
            }
        except (TypeError, ValueError):
            return {}
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return {
                "x": float(value[0]),
                "y": float(value[1]),
                "width": float(value[2]),
                "height": float(value[3]),
            }
        except (TypeError, ValueError):
            return {}
    return {}


def _point_to_pair(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if hasattr(value, "x") and hasattr(value, "y"):
        try:
            return float(value.x), float(value.y)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        try:
            return float(value.get("x", value.get("X"))), float(value.get("y", value.get("Y")))
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _size_to_pair(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if hasattr(value, "width") and hasattr(value, "height"):
        try:
            return float(value.width), float(value.height)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        try:
            return float(value.get("width", value.get("Width"))), float(value.get("height", value.get("Height")))
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _score_candidate(
    element: dict[str, Any],
    *,
    point: tuple[int, int] | None,
    intent: str | None,
) -> float:
    score = 0.0
    frame = element.get("frame") or {}
    if point is not None and _contains_point(frame, point):
        area = max(float(frame.get("width", 0)) * float(frame.get("height", 0)), 1.0)
        score += 100.0 + min(20.0, 20000.0 / area)
    if intent:
        score += _intent_score(element, intent)
    if "AXPress" in element.get("actions", []):
        score += 5.0
    if element.get("enabled") is False:
        score -= 1000.0
    return score


def _intent_score(element: dict[str, Any], intent: str) -> float:
    text = " ".join(
        str(element.get(key, ""))
        for key in ("title", "description", "role", "value")
        if element.get(key) is not None
    )
    haystack = _norm(text)
    intent_norm = _norm(intent)
    words = [word for word in re.findall(r"[a-z0-9]+", intent_norm) if word not in _STOP_WORDS]
    score = 0.0
    if intent_norm and intent_norm in haystack:
        score += 50.0
    for word in words:
        if word in haystack:
            score += 12.0
    role = _norm(element.get("role"))
    actions = set(element.get("actions", []))
    if any(word in intent_norm for word in ("press", "click", "open", "select")):
        if "AXPress" in actions:
            score += 15.0
        if "button" in role or "menuitem" in role or "checkbox" in role:
            score += 10.0
    if any(word in intent_norm for word in ("type", "set", "enter", "fill")):
        if "textfield" in role or "textarea" in role:
            score += 20.0
    return score


def _contains_point(frame: Any, point: tuple[int, int]) -> bool:
    if not isinstance(frame, dict):
        return False
    try:
        x = float(frame.get("x", 0))
        y = float(frame.get("y", 0))
        width = float(frame.get("width", 0))
        height = float(frame.get("height", 0))
        px, py = point
        return width > 0 and height > 0 and x <= px <= x + width and y <= py <= y + height
    except (TypeError, ValueError):
        return False


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _safe_enabled(value: Any) -> bool:
    if value is None:
        return True
    return bool(value)


def _safe_value(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in list(value.items())[:20]}
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
