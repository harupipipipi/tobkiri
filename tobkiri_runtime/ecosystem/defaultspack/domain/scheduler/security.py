from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.runtime_config import (
    scheduler_config,
    scheduler_jobs_path_override,
    tool_policy_config,
)


class SchedulerPolicyError(PermissionError):
    pass


def scheduler_enabled() -> bool:
    return scheduler_config().get("enabled", True) is not False


def resolve_jobs_path() -> Path | None:
    """Resolve an explicitly configured scheduler jobs file, if any."""
    raw = scheduler_jobs_path_override()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def validate_scheduler_enabled() -> None:
    if not scheduler_enabled():
        raise SchedulerPolicyError("scheduler is disabled by runtime_config")


def validate_no_agent_job(job: dict[str, Any]) -> None:
    if not (job.get("no_agent") or job.get("script")):
        return
    validate_scheduler_enabled()
    if tool_policy_config().get("allow_shell") is not True:
        raise SchedulerPolicyError("no_agent script jobs require runtime_config.tool_policy.allow_shell=true")
    if scheduler_config().get("allow_no_agent_scripts") is not True:
        raise SchedulerPolicyError("no_agent script jobs require runtime_config.scheduler.allow_no_agent_scripts=true")
    script = job.get("script")
    if script is not None and not isinstance(script, list):
        raise SchedulerPolicyError("no_agent script must be an argv list; shell strings are not allowed")


def validate_no_agent_argv(job: dict[str, Any]) -> list[str]:
    validate_no_agent_job(job)
    script = job.get("script")
    if not isinstance(script, list) or not script or not all(isinstance(item, str) and item for item in script):
        raise SchedulerPolicyError("no_agent script must be a non-empty argv list")
    allowlist = scheduler_config().get("no_agent_command_allowlist") or []
    allowed = {str(item) for item in allowlist if str(item)}
    executable = script[0]
    if allowed and executable not in allowed:
        raise SchedulerPolicyError("no_agent command is not allowlisted: " + executable)
    if not allowed:
        raise SchedulerPolicyError("no_agent command allowlist is empty")
    return list(script)
