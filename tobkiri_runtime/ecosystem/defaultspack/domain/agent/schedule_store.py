"""
domain/agent/schedule_store.py - Schedule persistence layer

Stores and loads schedule definitions as individual JSON files
under user_data/shared/schedules/.
Each schedule is stored as {schedule_id}.json.
Execution history is stored as {schedule_id}_history.json.
"""

import errno
import json
import math
import os
import tempfile
import threading
import time


_SCHEDULES_DIR = os.path.join("user_data", "shared", "schedules")
_lock = threading.Lock()


def _schedules_dir():
    override = os.environ.get("RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR", "").strip()
    if override:
        return override
    return _SCHEDULES_DIR


def current_schedules_dir():
    """Return the absolute directory currently used for schedule persistence."""
    return os.path.abspath(_schedules_dir())


def _sanitize_json_text(value):
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_sanitize_json_text(item) for item in value]
    if isinstance(value, dict):
        return {
            _sanitize_json_text(key): _sanitize_json_text(item)
            for key, item in value.items()
        }
    return value


def _json_safe(value):
    if isinstance(value, str):
        return _sanitize_json_text(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return str(value)
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            str(_sanitize_json_text(key)): _json_safe(item)
            for key, item in value.items()
        }
    return _sanitize_json_text(str(value))


def _is_transient_replace_error(exc):
    winerror = getattr(exc, "winerror", None)
    errno_value = getattr(exc, "errno", None)
    if isinstance(exc, PermissionError):
        return True
    if winerror in {5, 32}:
        return True
    if errno_value in {errno.EACCES, errno.EBUSY, errno.EPERM}:
        return True
    message = str(exc).lower()
    return "access is denied" in message or "permission denied" in message


def _replace_atomic_file(tmp_path, path):
    last_error = None
    for attempt in range(8):
        try:
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            last_error = exc
            if not _is_transient_replace_error(exc) or attempt >= 7:
                break
            time.sleep(min(0.05 * (2 ** attempt), 0.5))
    raise last_error


def _write_json_atomic(path, data):
    parent_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=parent_dir,
        prefix="." + os.path.basename(path) + ".",
        suffix=".tmp",
    )
    safe_data = _json_safe(data)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(safe_data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        _replace_atomic_file(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _skip_json_whitespace(text, index):
    length = len(text)
    while index < length and text[index] in " \t\r\n":
        index += 1
    return index


def _find_next_json_object_start(text, index):
    in_string = False
    escaped = False
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                return index
            elif char == "]":
                return -1
        index += 1
    return -1


def _recover_history_entries(text):
    """Best-effort recovery for legacy malformed history arrays."""
    decoder = json.JSONDecoder()
    entries = []
    index = _skip_json_whitespace(text, 0)
    if index >= len(text) or text[index] != "[":
        return entries
    index += 1
    while index < len(text):
        index = _skip_json_whitespace(text, index)
        if index >= len(text) or text[index] == "]":
            break
        if text[index] == ",":
            index += 1
            continue
        if text[index] != "{":
            next_index = _find_next_json_object_start(text, index + 1)
            if next_index < 0:
                break
            index = next_index
        try:
            entry, next_index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            next_index = _find_next_json_object_start(text, index + 1)
            if next_index < 0:
                break
            index = next_index
            continue
        if isinstance(entry, dict):
            entries.append(_json_safe(entry))
        index = next_index
    return entries


def _load_history_unlocked(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (json.JSONDecodeError, OSError):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return _recover_history_entries(f.read())
        except OSError:
            return []
    if not isinstance(history, list):
        return []
    return [_json_safe(entry) for entry in history if isinstance(entry, dict)]


def _ensure_dir():
    """Create the schedules directory if it does not exist."""
    schedules_dir = _schedules_dir()
    if not os.path.isdir(schedules_dir):
        os.makedirs(schedules_dir, exist_ok=True)


def _schedule_path(schedule_id):
    """Return the file path for a given schedule ID."""
    return os.path.join(_schedules_dir(), schedule_id + ".json")


def _history_path(schedule_id):
    """Return the file path for a given schedule's execution history."""
    return os.path.join(_schedules_dir(), schedule_id + "_history.json")


def save_schedule(schedule_dict):
    """Persist a schedule dict to disk. Overwrites if exists."""
    _ensure_dir()
    sid = schedule_dict.get("id")
    if not sid:
        raise ValueError("schedule dict must have an 'id' field")
    path = _schedule_path(sid)
    with _lock:
        _write_json_atomic(path, schedule_dict)


def load_schedule(schedule_id):
    """Load a single schedule from disk. Returns None if not found."""
    path = _schedule_path(schedule_id)
    with _lock:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def delete_schedule(schedule_id):
    """Remove a schedule file from disk. Returns True if deleted, False if not found."""
    path = _schedule_path(schedule_id)
    hist = _history_path(schedule_id)
    with _lock:
        found = False
        if os.path.isfile(path):
            os.remove(path)
            found = True
        if os.path.isfile(hist):
            os.remove(hist)
        return found


def load_all_schedules():
    """Load all schedule dicts from disk. Returns a list."""
    _ensure_dir()
    results = []
    with _lock:
        schedules_dir = _schedules_dir()
        for fname in os.listdir(schedules_dir):
            if fname.endswith(".json") and not fname.endswith("_history.json"):
                fpath = os.path.join(schedules_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    results.append(data)
                except (json.JSONDecodeError, OSError):
                    continue
    return results


def append_history(schedule_id, entry):
    """Append an execution history entry for a schedule.

    entry is a dict with at minimum: execution_id, started_at, status.
    History is capped at 200 entries (oldest trimmed).
    """
    _ensure_dir()
    path = _history_path(schedule_id)
    max_entries = 200
    with _lock:
        history = _load_history_unlocked(path)
        # Scheduler startup and a profile runtime bootstrap can observe the
        # same orphaned execution in either order.  Recovery is identified by
        # the execution id, so recording it twice would turn one restart into
        # two visible runs.  Keep ordinary execution history append-only; only
        # the explicitly marked obsolete recovery records are idempotent.
        if (
            isinstance(entry, dict)
            and entry.get("status") == "obsolete"
            and entry.get("recovered_obsolete_running_execution") is True
        ):
            execution_id = str(entry.get("execution_id") or "").strip()
            if execution_id and any(
                isinstance(item, dict)
                and str(item.get("execution_id") or "").strip() == execution_id
                and item.get("status") == "obsolete"
                and item.get("recovered_obsolete_running_execution") is True
                for item in history
            ):
                return
        history.append(_json_safe(entry))
        if len(history) > max_entries:
            history = history[-max_entries:]
        _write_json_atomic(path, history)


def load_history(schedule_id, limit=50, offset=0):
    """Load execution history for a schedule. Returns (entries, total_count)."""
    path = _history_path(schedule_id)
    with _lock:
        if not os.path.isfile(path):
            return [], 0
        history = _load_history_unlocked(path)
    if not history:
        return [], 0
    total = len(history)
    # Return in reverse chronological order
    reversed_hist = list(reversed(history))
    page = reversed_hist[offset:offset + limit]
    return page, total
