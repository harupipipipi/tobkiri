"""
domain/agent/scheduler.py - In-memory scheduler engine

Manages scheduled agent executions using threading.Timer.
Supports three schedule types:
  - interval: execute every N seconds/minutes/hours
  - cron: execute according to a cron expression (min hour dom month dow)
  - once: execute at a specific datetime then auto-disable

No external dependencies. Pure stdlib.
"""

import sys
import os
import threading
import time
import calendar
import re
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import gen_id, timestamp
from domain.agent.schedule_store import (
    save_schedule,
    load_schedule,
    load_all_schedules,
    delete_schedule as store_delete,
    append_history,
    load_history,
)


_APPROVAL_FINISH_REASONS = {"approval_required", "authority_approval_required"}


# ---------------------------------------------------------------------------
# Minimal cron expression parser (5-field: minute hour day month weekday)
# ---------------------------------------------------------------------------

def _parse_cron_field(field, min_val, max_val):
    """Parse a single cron field into a set of integers.

    Supports: *, N, N-M, N-M/S, */S, N,M,O
    """
    result = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        # Handle */N
        if part.startswith("*/"):
            step_str = part[2:]
            if not step_str.isdigit():
                raise ValueError("invalid cron step: " + part)
            step = int(step_str)
            if step < 1:
                raise ValueError("cron step must be >= 1")
            for v in range(min_val, max_val + 1, step):
                result.add(v)
            continue
        # Handle *
        if part == "*":
            for v in range(min_val, max_val + 1):
                result.add(v)
            continue
        # Handle N-M or N-M/S
        range_match = re.match(r"^(\d+)-(\d+)(?:/(\d+))?$", part)
        if range_match:
            lo = int(range_match.group(1))
            hi = int(range_match.group(2))
            step = int(range_match.group(3)) if range_match.group(3) else 1
            if lo < min_val or hi > max_val or lo > hi or step < 1:
                raise ValueError("invalid cron range: " + part)
            for v in range(lo, hi + 1, step):
                result.add(v)
            continue
        # Handle plain number
        if part.isdigit():
            v = int(part)
            if v < min_val or v > max_val:
                raise ValueError("cron value out of range: " + part)
            result.add(v)
            continue
        raise ValueError("invalid cron token: " + part)
    return result


def parse_cron_expression(expr):
    """Parse a 5-field cron expression. Returns dict with sets for each field.

    Fields: minute(0-59) hour(0-23) day(1-31) month(1-12) weekday(0-6, 0=Sun)
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError("cron expression must have exactly 5 fields, got " + str(len(parts)))
    return {
        "minute": _parse_cron_field(parts[0], 0, 59),
        "hour": _parse_cron_field(parts[1], 0, 23),
        "day": _parse_cron_field(parts[2], 1, 31),
        "month": _parse_cron_field(parts[3], 1, 12),
        "weekday": _parse_cron_field(parts[4], 0, 6),
    }


def cron_matches(parsed_cron, dt):
    """Check if a datetime matches a parsed cron expression."""
    # weekday: Python Monday=0 ... Sunday=6; cron Sunday=0
    cron_dow = (dt.weekday() + 1) % 7  # Convert Python weekday to cron weekday
    return (
        dt.minute in parsed_cron["minute"]
        and dt.hour in parsed_cron["hour"]
        and dt.day in parsed_cron["day"]
        and dt.month in parsed_cron["month"]
        and cron_dow in parsed_cron["weekday"]
    )


def next_cron_time(parsed_cron, from_dt):
    """Find the next datetime after from_dt that matches the cron expression.

    Searches up to 366 days ahead. Returns a datetime or None.
    """
    # Start from the next minute
    candidate = from_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = from_dt + timedelta(days=366)
    while candidate < limit:
        if cron_matches(parsed_cron, candidate):
            return candidate
        candidate += timedelta(minutes=1)
        # Optimisation: if current hour is not in cron hours, skip to next hour
        cron_dow = (candidate.weekday() + 1) % 7
        if candidate.month not in parsed_cron["month"]:
            # Skip to first day of next month
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                candidate = candidate.replace(month=candidate.month + 1, day=1, hour=0, minute=0)
            continue
        if candidate.day not in parsed_cron["day"] or cron_dow not in parsed_cron["weekday"]:
            # Skip to next day
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if candidate.hour not in parsed_cron["hour"]:
            # Skip to next hour
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
    return None


def _interval_to_seconds(value, unit):
    """Convert an interval value+unit to seconds."""
    multipliers = {"seconds": 1, "minutes": 60, "hours": 3600}
    if unit not in multipliers:
        raise ValueError("unit must be one of: seconds, minutes, hours")
    return value * multipliers[unit]


def _parse_iso_datetime(dt_str):
    """Parse an ISO 8601 datetime string to a UTC datetime object."""
    # Handle Z suffix
    s = dt_str.replace("Z", "+00:00")
    # Python 3.7+ fromisoformat handles timezone offsets
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        # Fallback: try strptime for common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(dt_str.rstrip("Z"), fmt.rstrip("%z"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        raise ValueError("cannot parse datetime: " + dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _success_status_from_data(data):
    if not isinstance(data, dict):
        return "completed"
    finish_reason = str(data.get("finish_reason") or "").strip()
    status = str(data.get("status") or "").strip()
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if (
        finish_reason in _APPROVAL_FINISH_REASONS
        or status in _APPROVAL_FINISH_REASONS
        or status in {"waiting_approval", "pending_approval"}
        or isinstance(metadata.get("pending_approval"), dict)
        or isinstance(metadata.get("pendingAuthorityApproval"), dict)
        or isinstance(metadata.get("pending_authority_approval"), dict)
    ):
        return "approval_required"
    return "completed"


def _content_from_result_data(data):
    if isinstance(data, dict):
        content = data.get("content", data.get("text", str(data)))
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("text")
            )
        return str(content)
    if isinstance(data, str):
        return data
    return str(data)


def _error_details_from_result(result):
    if not isinstance(result, dict):
        return str(result), "INVALID_RESULT"
    err = result.get("error", {})
    if isinstance(err, dict):
        return str(err.get("message") or err), str(err.get("code") or "")
    return str(err), ""


# ---------------------------------------------------------------------------
# Scheduler singleton
# ---------------------------------------------------------------------------

class Scheduler:
    """In-memory scheduler backed by threading.Timer.

    Each schedule has a corresponding Timer that fires at the next execution time.
    When the timer fires, the task is executed and the timer is re-armed for the
    next occurrence (unless the schedule type is 'once').
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False
            return cls._instance

    def __init__(self):
        if self._initialised:
            return
        self._initialised = True
        self._lock = threading.Lock()
        self._timers = {}        # schedule_id -> threading.Timer
        self._schedules = {}     # schedule_id -> schedule dict (in-memory cache)
        self._active_executions = {}  # schedule_id -> execution_id -> running metadata
        self._loaded = False

    # ---- public API ----

    def ensure_loaded(self):
        """Load all schedules from disk and arm timers for active ones."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
        all_scheds = load_all_schedules()
        for sd in all_scheds:
            sid = sd.get("id")
            if not sid:
                continue
            with self._lock:
                self._schedules[sid] = sd
            if sd.get("status") == "active":
                self._arm_timer(sid)

    def create_schedule(self, schedule_type, task_config, schedule_config, name="", description=""):
        """Create and persist a new schedule.

        schedule_type: "interval" | "cron" | "once"
        task_config: {message, model, conversation_id, timeout}
        schedule_config: depends on type
          - interval: {value: int, unit: "seconds"|"minutes"|"hours"}
          - cron: {expression: "0 9 * * *"}
          - once: {run_at: "2025-03-01T09:00:00Z"}
        """
        self.ensure_loaded()

        # Validate schedule_type
        if schedule_type not in ("interval", "cron", "once"):
            raise ValueError("schedule_type must be one of: interval, cron, once")

        # Validate task_config
        if not isinstance(task_config, dict):
            raise ValueError("task_config must be a dict")
        if not task_config.get("message"):
            raise ValueError("task_config.message is required")

        # Validate schedule_config
        if not isinstance(schedule_config, dict):
            raise ValueError("schedule_config must be a dict")

        if schedule_type == "interval":
            val = schedule_config.get("value")
            unit = schedule_config.get("unit", "minutes")
            if not isinstance(val, (int, float)) or val <= 0:
                raise ValueError("schedule_config.value must be a positive number")
            _interval_to_seconds(val, unit)  # validates unit
        elif schedule_type == "cron":
            expr = schedule_config.get("expression")
            if not expr:
                raise ValueError("schedule_config.expression is required for cron type")
            parse_cron_expression(expr)  # validates expression
        elif schedule_type == "once":
            run_at = schedule_config.get("run_at")
            if not run_at:
                raise ValueError("schedule_config.run_at is required for once type")
            _parse_iso_datetime(run_at)  # validates datetime

        now = timestamp()
        sid = "sched_" + gen_id()

        task = {
            "message": task_config.get("message"),
            "model": task_config.get("model", "default"),
            "conversation_id": task_config.get("conversation_id"),
            "timeout": task_config.get("timeout", 300),
        }
        for key in ("profile_id", "agent_id", "tools", "tool_policy", "metadata", "thinking_level"):
            if key in task_config:
                task[key] = task_config.get(key)

        schedule = {
            "id": sid,
            "name": name if name else "Schedule " + sid[:12],
            "description": description,
            "type": schedule_type,
            "task": task,
            "config": schedule_config,
            "status": "active",
            "execution_count": 0,
            "last_executed_at": None,
            "next_execution_at": None,
            "created_at": now,
            "updated_at": now,
        }

        # Compute next execution time
        schedule["next_execution_at"] = self._compute_next_execution(schedule)

        save_schedule(schedule)
        with self._lock:
            self._schedules[sid] = schedule

        self._arm_timer(sid)
        return schedule

    def get_schedule(self, schedule_id):
        """Return a schedule dict or None."""
        self.ensure_loaded()
        with self._lock:
            return self._schedules.get(schedule_id)

    def list_schedules(self, status_filter=None):
        """Return list of all schedules, optionally filtered by status."""
        self.ensure_loaded()
        with self._lock:
            all_s = list(self._schedules.values())
        if status_filter:
            all_s = [s for s in all_s if s.get("status") == status_filter]
        all_s.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return all_s

    def update_schedule(self, schedule_id, updates):
        """Update a schedule. Allowed fields: name, description, task, config, type.

        Returns the updated schedule dict or None if not found.
        """
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None

        allowed_keys = ("name", "description", "task", "config", "type")
        changed = False
        for key in allowed_keys:
            if key in updates:
                if key == "type":
                    if updates[key] not in ("interval", "cron", "once"):
                        raise ValueError("type must be one of: interval, cron, once")
                if key == "config":
                    # Re-validate config based on type
                    stype = updates.get("type", sched.get("type"))
                    cfg = updates["config"]
                    if stype == "interval":
                        val = cfg.get("value")
                        unit = cfg.get("unit", "minutes")
                        if not isinstance(val, (int, float)) or val <= 0:
                            raise ValueError("config.value must be a positive number")
                        _interval_to_seconds(val, unit)
                    elif stype == "cron":
                        expr = cfg.get("expression")
                        if not expr:
                            raise ValueError("config.expression is required for cron type")
                        parse_cron_expression(expr)
                    elif stype == "once":
                        run_at = cfg.get("run_at")
                        if not run_at:
                            raise ValueError("config.run_at is required for once type")
                        _parse_iso_datetime(run_at)
                if key == "task":
                    if not isinstance(updates["task"], dict):
                        raise ValueError("task must be a dict")
                    # Merge with existing task
                    merged_task = dict(sched.get("task", {}))
                    merged_task.update(updates["task"])
                    if not merged_task.get("message"):
                        raise ValueError("task.message cannot be empty")
                    sched["task"] = merged_task
                    changed = True
                    continue
                sched[key] = updates[key]
                changed = True

        if changed:
            sched["updated_at"] = timestamp()
            if sched.get("status") == "active":
                sched["next_execution_at"] = self._compute_next_execution(sched)
            save_schedule(sched)
            with self._lock:
                self._schedules[schedule_id] = sched
            if sched.get("status") == "active":
                self._cancel_timer(schedule_id)
                self._arm_timer(schedule_id)

        return sched

    def delete_schedule(self, schedule_id):
        """Delete a schedule. Returns True if deleted."""
        self.ensure_loaded()
        self._cancel_timer(schedule_id)
        with self._lock:
            removed = self._schedules.pop(schedule_id, None)
        store_delete(schedule_id)
        return removed is not None

    def pause_schedule(self, schedule_id):
        """Pause an active schedule. Returns updated schedule or None."""
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None
        if sched.get("status") != "active":
            return sched  # already not active
        self._cancel_timer(schedule_id)
        sched["status"] = "paused"
        sched["next_execution_at"] = None
        sched["updated_at"] = timestamp()
        save_schedule(sched)
        with self._lock:
            self._schedules[schedule_id] = sched
        return sched

    def resume_schedule(self, schedule_id):
        """Resume a paused schedule. Returns updated schedule or None."""
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None
        if sched.get("status") == "active":
            return sched  # already active
        if sched.get("status") == "completed":
            return sched  # once-type that already ran, cannot resume
        sched["status"] = "active"
        sched["next_execution_at"] = self._compute_next_execution(sched)
        sched["updated_at"] = timestamp()
        save_schedule(sched)
        with self._lock:
            self._schedules[schedule_id] = sched
        self._arm_timer(schedule_id)
        return sched

    def trigger_now(self, schedule_id):
        """Manually trigger a schedule execution immediately.

        Returns execution history entry.
        """
        self.ensure_loaded()
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None
        return self._execute_task(schedule_id, manual=True)

    def get_history(self, schedule_id, limit=50, offset=0):
        """Return execution history for a schedule."""
        self.ensure_loaded()
        entries, total = load_history(schedule_id, limit=limit, offset=offset)
        return {"entries": entries, "total": total, "limit": limit, "offset": offset}

    # ---- internal ----

    def _compute_next_execution(self, sched):
        """Compute the ISO timestamp of the next execution."""
        stype = sched.get("type")
        cfg = sched.get("config", {})
        now = datetime.now(timezone.utc)

        if stype == "interval":
            val = cfg.get("value", 60)
            unit = cfg.get("unit", "minutes")
            secs = _interval_to_seconds(val, unit)
            nxt = now + timedelta(seconds=secs)
            return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if stype == "cron":
            expr = cfg.get("expression", "* * * * *")
            parsed = parse_cron_expression(expr)
            nxt = next_cron_time(parsed, now)
            if nxt is None:
                return None
            return nxt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if stype == "once":
            run_at = cfg.get("run_at")
            if not run_at:
                return None
            dt = _parse_iso_datetime(run_at)
            if dt <= now:
                return None  # already past
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        return None

    def _seconds_until_next(self, sched):
        """Return seconds until the next scheduled execution, or None."""
        nxt_str = sched.get("next_execution_at")
        if not nxt_str:
            return None
        try:
            nxt = _parse_iso_datetime(nxt_str)
        except ValueError:
            return None
        now = datetime.now(timezone.utc)
        delta = (nxt - now).total_seconds()
        if delta < 0:
            return 0.1  # fire immediately if overdue
        return max(delta, 0.1)

    def _arm_timer(self, schedule_id):
        """Set a threading.Timer for the next execution of a schedule."""
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active":
            return
        delay = self._seconds_until_next(sched)
        if delay is None:
            return
        # Cap very long delays at 1 hour and re-check then
        max_delay = 3600.0
        if delay > max_delay:
            timer = threading.Timer(max_delay, self._recheck_and_arm, args=[schedule_id])
        else:
            timer = threading.Timer(delay, self._on_timer_fire, args=[schedule_id])
        timer.daemon = True
        with self._lock:
            old = self._timers.pop(schedule_id, None)
            if old is not None:
                old.cancel()
            self._timers[schedule_id] = timer
        timer.start()

    def _recheck_and_arm(self, schedule_id):
        """Called when delay was capped; re-compute and re-arm."""
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active":
            return
        self._arm_timer(schedule_id)

    def _cancel_timer(self, schedule_id):
        """Cancel any running timer for a schedule."""
        with self._lock:
            timer = self._timers.pop(schedule_id, None)
        if timer is not None:
            timer.cancel()

    def _mark_running_execution(self, schedule_id: str, history_entry: dict) -> None:
        with self._lock:
            sched = self._schedules.get(schedule_id)
            if sched is None:
                return
            updated = dict(sched)
            running = {
                "execution_id": history_entry.get("execution_id"),
                "started_at": history_entry.get("started_at"),
                "trigger": history_entry.get("trigger"),
                "status": "running",
            }
            updated["running_execution"] = running
            updated["updated_at"] = timestamp()
            save_schedule(updated)
            self._active_executions.setdefault(schedule_id, {})[
                running["execution_id"]
            ] = running
            self._schedules[schedule_id] = updated

    def _finish_execution(
        self, schedule_id: str, exec_id: str, history_entry: dict
    ) -> None:
        with self._lock:
            sched = self._schedules.get(schedule_id)
            active = self._active_executions.get(schedule_id, {})
            active.pop(exec_id, None)
            if not active:
                self._active_executions.pop(schedule_id, None)
            if sched is not None:
                updated = dict(sched)
                running = updated.get("running_execution")
                if active:
                    updated["running_execution"] = next(reversed(active.values()))
                elif (
                    not isinstance(running, dict)
                    or running.get("execution_id") == exec_id
                ):
                    updated.pop("running_execution", None)
                updated["execution_count"] = updated.get("execution_count", 0) + 1
                updated["last_executed_at"] = history_entry["completed_at"]
                updated["last_execution_status"] = history_entry.get("status")
                if history_entry.get("error"):
                    updated["last_execution_error"] = history_entry.get("error")
                else:
                    updated.pop("last_execution_error", None)
                updated["updated_at"] = timestamp()
                save_schedule(updated)
                self._schedules[schedule_id] = updated
        append_history(schedule_id, history_entry)

    def _on_timer_fire(self, schedule_id):
        """Called when a timer fires. Execute the task and re-arm."""
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None or sched.get("status") != "active":
            return
        self._execute_task(schedule_id, manual=False)
        # Re-arm for next execution (interval / cron). For 'once', mark completed.
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return
        if sched.get("type") == "once":
            sched["status"] = "completed"
            sched["next_execution_at"] = None
            sched["updated_at"] = timestamp()
            save_schedule(sched)
            with self._lock:
                self._schedules[schedule_id] = sched
        else:
            sched["next_execution_at"] = self._compute_next_execution(sched)
            sched["updated_at"] = timestamp()
            save_schedule(sched)
            with self._lock:
                self._schedules[schedule_id] = sched
            self._arm_timer(schedule_id)

    def _execute_task(self, schedule_id, manual=False):
        """Execute the agent task for a schedule and record history."""
        with self._lock:
            sched = self._schedules.get(schedule_id)
        if sched is None:
            return None

        task_cfg = sched.get("task", {})
        message = task_cfg.get("message", "")
        model = task_cfg.get("model", "default")
        timeout = task_cfg.get("timeout", 300)
        conversation_id = task_cfg.get("conversation_id")

        exec_id = "sexec_" + gen_id()
        started_at = timestamp()

        history_entry = {
            "execution_id": exec_id,
            "schedule_id": schedule_id,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "trigger": "manual" if manual else "scheduled",
            "result": None,
            "error": None,
        }

        self._mark_running_execution(schedule_id, history_entry)

        try:
            if conversation_id:
                from blocks.chat.send import run as chat_send_run

                params = {}
                if task_cfg.get("model"):
                    params["model"] = task_cfg.get("model")
                if isinstance(task_cfg.get("tool_policy"), dict):
                    params["tool_policy"] = task_cfg["tool_policy"]
                if task_cfg.get("thinking_level"):
                    params["thinking_level"] = task_cfg.get("thinking_level")
                metadata = task_cfg.get("metadata") if isinstance(task_cfg.get("metadata"), dict) else {}
                result = chat_send_run(
                    {
                        "conversation_id": conversation_id,
                        "message": {
                            "role": "user",
                            "content": message,
                            "metadata": {
                                **metadata,
                                "source": "scheduler",
                                "schedule_id": schedule_id,
                                "schedule_execution_id": exec_id,
                                "trigger": "manual" if manual else "scheduled",
                                "profile_id": task_cfg.get("profile_id"),
                                "agent_id": task_cfg.get("agent_id"),
                            },
                        },
                        "params": params,
                        "tools": task_cfg.get("tools") if isinstance(task_cfg.get("tools"), list) else None,
                    },
                    {"profile_policy": task_cfg.get("tool_policy") if isinstance(task_cfg.get("tool_policy"), dict) else {}},
                )
            else:
                from blocks.ai.complete import run as ai_complete_run

                messages = []
                system_content = (
                    "You are a scheduled agent. Execute the following task. "
                    "Be concise and precise in your response."
                )
                messages.append({"role": "system", "content": system_content})
                messages.append({"role": "user", "content": message})

                empty_context = {}
                result = ai_complete_run({"messages": messages, "model": model}, empty_context)

            if isinstance(result, dict) and result.get("status") == "ok":
                data = result.get("data", {})
                history_entry["status"] = _success_status_from_data(data)
                history_entry["result"] = _content_from_result_data(data)
            else:
                err_msg, err_code = _error_details_from_result(result)
                history_entry["status"] = "error"
                history_entry["error"] = err_msg
                if err_code:
                    history_entry["error_code"] = err_code

        except Exception as exc:
            history_entry["status"] = "error"
            history_entry["error"] = str(exc)

        history_entry["completed_at"] = timestamp()
        self._finish_execution(schedule_id, exec_id, history_entry)

        return history_entry

    def shutdown(self):
        """Cancel all timers. Called on process exit."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
